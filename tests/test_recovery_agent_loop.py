from __future__ import annotations

import copy
import json
import unittest

from langchain_core.messages import AIMessage

from agent_core.workflow.recovery_agent import (
    RecoveryAgentLoop,
    RecoveryAgentLoopConfig,
)
from agent_core.workflow.recovery_projection import (
    build_causal_failure_record,
    project_recovery_seed,
)
from agent_core.workflow.scoped_context import provider_visible_payload
from tests.test_recovery_projection import _durable, _execute_failure


def _seed_fixture():
    durable = _durable()
    record = build_causal_failure_record(
        "execute_outcome", _execute_failure(), durable, task_id="task-recovery"
    )
    task_state = dict(durable["task_state"])
    task_state["causal_failure_packets"] = {record["packet_ref"]: record}
    task_state["active_failure_packet_ref"] = record["packet_ref"]
    durable["task_state"] = task_state
    projection = project_recovery_seed({
        "task_id": "task-recovery",
        "active_failure_packet_ref": record["packet_ref"],
        "failure_packet": record["failure_packet"],
        "failure_source": record["failure_source"],
    }, durable)
    assert projection is not None
    return durable, projection.recovery_seed


def _directive() -> str:
    return json.dumps({
        "schema_version": 1,
        "directive_id": "directive-1",
        "task_id": "task-recovery",
        "action": "retry_step",
        "summary": "The generated action failed and must be regenerated.",
        "reason_code": "GENERATED_ACTION_FAILED",
        "target_step_id": "step-1",
        "instruction": "Regenerate the current action using the verified runtime facts.",
        "evidence_refs": ["execution-1"],
    })


class _Dispatcher:
    def __init__(self, *, repeated: bool = False) -> None:
        self.calls = []
        self.repeated = repeated

    def dispatch(self, tool, args):
        self.calls.append((tool, copy.deepcopy(dict(args))))
        return {
            "status": "ok",
            "evidence_ref": "execution-1",
            "fact": "return code 17",
        }


class _ToolThenDirectiveModel:
    def __init__(self, durable=None):
        self.requests = []
        self.durable = durable

    async def ainvoke(self, messages):
        self.requests.append([provider_visible_payload(item) for item in messages])
        if len(self.requests) == 1:
            if self.durable is not None:
                self.durable["messages"] = ["global history must stay excluded"]
                self.durable["current_task"]["task_spec"]["goal"] = "mutated after seed"
            return AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-1",
                    "name": "failure_inspect",
                    "args": {},
                    "type": "tool_call",
                }],
            )
        return AIMessage(content=_directive())


class RecoveryAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_episode_is_exact_append_only_with_one_frozen_seed(self) -> None:
        durable, seed = _seed_fixture()
        durable["messages"] = [{"role": "user", "content": "old global history"}]
        model = _ToolThenDirectiveModel(durable)
        dispatcher = _Dispatcher()
        result = await RecoveryAgentLoop(
            model=model, dispatcher=dispatcher, bind_tools=False
        ).ainvoke(seed=seed, durable=durable)

        self.assertFalse(result["terminal_error"])
        self.assertEqual(result["directive"]["action"], "retry_step")
        self.assertEqual(dispatcher.calls, [("failure.inspect", {})])
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(
            model.requests[1][:len(model.requests[0])], model.requests[0]
        )
        self.assertEqual(len(model.requests[0]), 2)
        self.assertEqual(len(model.requests[1]), 4)
        self.assertEqual(model.requests[0][1], model.requests[1][1])
        wire = json.dumps(model.requests, ensure_ascii=False)
        self.assertNotIn("old global history", wire)
        self.assertNotIn("mutated after seed", wire)
        self.assertTrue(result["audit"]["append_only"])

    async def test_transport_retry_reuses_identical_message_array(self) -> None:
        durable, seed = _seed_fixture()

        class Flaky:
            def __init__(self):
                self.requests = []

            async def ainvoke(self, messages):
                self.requests.append([
                    provider_visible_payload(item) for item in messages
                ])
                if len(self.requests) == 1:
                    raise TimeoutError("fixture timeout")
                return AIMessage(content=_directive())

        model = Flaky()
        result = await RecoveryAgentLoop(
            model=model,
            dispatcher=_Dispatcher(),
            config=RecoveryAgentLoopConfig(max_transport_attempts=2),
            bind_tools=False,
        ).ainvoke(seed=seed, durable=durable)
        self.assertFalse(result["terminal_error"])
        self.assertEqual(model.requests[0], model.requests[1])
        self.assertEqual(result["audit"]["transport_attempt_count"], 2)

    async def test_exact_repeat_budget_stops_third_identical_request(self) -> None:
        durable, seed = _seed_fixture()

        class Repeat:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, _messages):
                self.calls += 1
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "id": f"repeat-{self.calls}",
                        "name": "failure_inspect",
                        "args": {},
                        "type": "tool_call",
                    }],
                )

        result = await RecoveryAgentLoop(
            model=Repeat(),
            dispatcher=_Dispatcher(),
            config=RecoveryAgentLoopConfig(
                max_exact_repeated_tool_request=2,
                max_consecutive_no_progress_rounds=3,
            ),
            bind_tools=False,
        ).ainvoke(seed=seed, durable=durable)
        self.assertEqual(
            result["terminal_error"]["code"],
            "RECOVERY_TOOL_REQUEST_REPEAT_LIMIT",
        )
        self.assertEqual(result["audit"]["tool_call_count"], 2)

    async def test_no_progress_budget_stops_repeated_observation(self) -> None:
        durable, seed = _seed_fixture()

        class Repeat:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, _messages):
                self.calls += 1
                return AIMessage(
                    content="",
                    tool_calls=[{
                        "id": f"no-progress-{self.calls}",
                        "name": "failure_inspect",
                        "args": {},
                        "type": "tool_call",
                    }],
                )

        result = await RecoveryAgentLoop(
            model=Repeat(),
            dispatcher=_Dispatcher(),
            config=RecoveryAgentLoopConfig(
                max_exact_repeated_tool_request=10,
                max_consecutive_no_progress_rounds=2,
            ),
            bind_tools=False,
        ).ainvoke(seed=seed, durable=durable)
        self.assertEqual(
            result["terminal_error"]["code"], "RECOVERY_NO_PROGRESS_LIMIT"
        )
        # First inspection is progress; the next two are identical.
        self.assertEqual(result["audit"]["tool_call_count"], 3)

    async def test_single_observation_is_replaced_by_narrowing_receipt(self) -> None:
        durable, seed = _seed_fixture()

        class LargeDispatcher:
            def dispatch(self, _tool, _args):
                return {"content": "x" * 8_000}

        model = _ToolThenDirectiveModel()
        result = await RecoveryAgentLoop(
            model=model,
            dispatcher=LargeDispatcher(),
            config=RecoveryAgentLoopConfig(
                single_observation_chars=2_000,
                cumulative_observation_chars=4_000,
            ),
            bind_tools=False,
        ).ainvoke(seed=seed, durable=durable)
        self.assertFalse(result["terminal_error"])
        second_request = json.dumps(model.requests[1], ensure_ascii=False)
        self.assertIn("REQUIRES_NARROWING", second_request)
        self.assertNotIn("x" * 1_000, second_request)
        self.assertLessEqual(result["audit"]["observation_chars"], 2_000)


if __name__ == "__main__":
    unittest.main()

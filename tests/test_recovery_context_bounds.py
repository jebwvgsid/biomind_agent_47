from __future__ import annotations

import json
import tempfile
import unittest

from langchain_core.messages import AIMessage

from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_agent import (
    RecoveryAgentError,
    RecoveryAgentLoop,
    RecoveryAgentLoopConfig,
)
from agent_core.workflow.recovery_context import RecoveryObservationStore
from tests.test_recovery_agent_loop import _directive, _seed_fixture


class _LargeDispatcher:
    def dispatch(self, _tool, _args):
        return {
            "status": "ok",
            "evidence_ref": "execution-1",
            "content": "x" * 12_000,
        }


class _ToolThenDirective:
    def __init__(self):
        self.requests = []

    async def ainvoke(self, messages):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            return AIMessage(content="", tool_calls=[{
                "id": "large-call",
                "name": "failure_inspect",
                "args": {},
                "type": "tool_call",
            }])
        return AIMessage(content=_directive())


class RecoveryContextBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_observation_is_externalized_to_immutable_archive(self) -> None:
        durable, seed = _seed_fixture()
        with tempfile.TemporaryDirectory(prefix="recovery-observation-") as root:
            phase_store = PhaseArchiveStore(root)
            model = _ToolThenDirective()
            result = await RecoveryAgentLoop(
                model=model,
                dispatcher=_LargeDispatcher(),
                observation_store=RecoveryObservationStore(phase_store),
                config=RecoveryAgentLoopConfig(
                    single_observation_chars=3_000,
                    cumulative_observation_chars=6_000,
                ),
                bind_tools=False,
            ).ainvoke(seed=seed, durable=durable)

            self.assertFalse(result["terminal_error"])
            tool_message = json.loads(str(model.requests[1][-1].content))
            receipt = tool_message["result"]
            self.assertEqual(receipt["status"], "EXTERNALIZED")
            self.assertLessEqual(
                result["audit"]["observation_chars"], 3_000
            )
            archived = phase_store.read(receipt["observation_ref"])
            self.assertEqual(archived.phase, "recovery-observation")
            self.assertEqual(
                archived.summary["content_sha256"], receipt["content_sha256"]
            )
            self.assertEqual(len(archived.summary["result"]["content"]), 12_000)

    async def test_soft_limit_externalizes_new_large_observation(self) -> None:
        durable, seed = _seed_fixture()
        with tempfile.TemporaryDirectory(prefix="recovery-soft-bound-") as root:
            model = _ToolThenDirective()
            result = await RecoveryAgentLoop(
                model=model,
                dispatcher=_LargeDispatcher(),
                observation_store=RecoveryObservationStore(
                    PhaseArchiveStore(root)
                ),
                config=RecoveryAgentLoopConfig(
                    local_context_soft_chars=1,
                    local_context_hard_chars=1_000_000,
                    single_observation_chars=24_000,
                    cumulative_observation_chars=96_000,
                ),
                bind_tools=False,
            ).ainvoke(seed=seed, durable=durable)
            self.assertTrue(result["audit"]["soft_limit_reached"])
            tool_message = json.loads(str(model.requests[1][-1].content))
            self.assertEqual(tool_message["result"]["status"], "EXTERNALIZED")

    async def test_hard_limit_emits_deterministic_checkpoint(self) -> None:
        durable, seed = _seed_fixture()
        config = RecoveryAgentLoopConfig(
            local_context_soft_chars=1,
            local_context_hard_chars=2,
        )
        first = await RecoveryAgentLoop(
            model=_ToolThenDirective(),
            dispatcher=_LargeDispatcher(),
            config=config,
            bind_tools=False,
        ).ainvoke(seed=seed, durable=durable)
        second = await RecoveryAgentLoop(
            model=_ToolThenDirective(),
            dispatcher=_LargeDispatcher(),
            config=config,
            bind_tools=False,
        ).ainvoke(seed=seed, durable=durable)
        self.assertEqual(
            first["terminal_error"]["code"],
            "RECOVERY_LOCAL_CONTEXT_HARD_LIMIT",
        )
        self.assertEqual(first["checkpoint"], second["checkpoint"])
        self.assertEqual(first["checkpoint"]["failure_id"], seed.failure_packet.failure_id)
        self.assertIn("budget_counters", first["checkpoint"])
        serialized = json.dumps(first["checkpoint"], ensure_ascii=False)
        self.assertNotIn("transcript", serialized)
        self.assertNotIn("reasoning", serialized)

    async def test_overflow_epoch_above_configured_max_fails_closed(self) -> None:
        durable, seed = _seed_fixture()
        with self.assertRaisesRegex(
            RecoveryAgentError, "RECOVERY_OVERFLOW_EPOCH_LIMIT"
        ):
            await RecoveryAgentLoop(
                model=_ToolThenDirective(),
                dispatcher=_LargeDispatcher(),
                bind_tools=False,
            ).ainvoke(seed=seed, durable=durable, overflow_epoch=3)


if __name__ == "__main__":
    unittest.main()

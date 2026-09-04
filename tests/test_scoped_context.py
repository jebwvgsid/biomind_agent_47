from __future__ import annotations

import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from agent_core.workflow.scoped_context import (
    PhaseContextInput,
    PhaseContextOutput,
    PhaseContextState,
    ScopedContextError,
    append_parallel_tool_messages,
    assert_stable_provider_prefix,
    build_provider_messages,
    fixed_provider_message,
    order_parallel_tool_messages,
    provider_visible_payload,
    strict_append_messages,
)


def _usage(input_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": 1,
        "total_tokens": input_tokens + 1,
    }


class StrictAppendReducerTests(unittest.TestCase):
    def test_new_ids_append_and_same_visible_replay_is_a_noop(self) -> None:
        original = AIMessage(
            id="assistant-1",
            content="inspect the artifact",
            name="worker",
            usage_metadata=_usage(10),
            response_metadata={"latency_ms": 120, "cache_hit_tokens": 0},
            additional_kwargs={"timestamp": "first"},
        )
        replay = AIMessage(
            id="assistant-1",
            content="inspect the artifact",
            name="worker",
            usage_metadata=_usage(99),
            response_metadata={"latency_ms": 1, "cache_hit_tokens": 9000},
            additional_kwargs={"timestamp": "retry", "trace_id": "different"},
        )
        next_message = HumanMessage(id="human-2", content="continue")

        result = strict_append_messages([original], [replay, next_message])

        self.assertEqual([message.id for message in result], ["assistant-1", "human-2"])
        self.assertIs(result[0], original)
        self.assertEqual(result[0].usage_metadata["input_tokens"], 10)
        self.assertEqual(result[0].response_metadata["latency_ms"], 120)

    def test_same_id_with_different_provider_payload_fails_closed(self) -> None:
        baseline = AIMessage(
            id="assistant-1",
            content="call tools",
            name="worker",
            tool_calls=[{
                "id": "call-1",
                "name": "inspect",
                "args": {"path": "a.txt"},
                "type": "tool_call",
            }],
        )
        conflicts = [
            AIMessage(
                id="assistant-1",
                content="different content",
                name="worker",
                tool_calls=baseline.tool_calls,
            ),
            AIMessage(
                id="assistant-1",
                content="call tools",
                name="different-name",
                tool_calls=baseline.tool_calls,
            ),
            AIMessage(
                id="assistant-1",
                content="call tools",
                name="worker",
                tool_calls=[{
                    "id": "call-1",
                    "name": "inspect",
                    "args": {"path": "b.txt"},
                    "type": "tool_call",
                }],
            ),
        ]
        for conflict in conflicts:
            with self.subTest(conflict=provider_visible_payload(conflict)):
                with self.assertRaisesRegex(
                    ScopedContextError, "APPEND_ONLY_MESSAGE_CONFLICT"
                ):
                    strict_append_messages([baseline], [conflict])

    def test_tool_call_id_is_provider_visible(self) -> None:
        first = ToolMessage(
            id="result-1", name="inspect", tool_call_id="call-1", content="ok"
        )
        changed = ToolMessage(
            id="result-1", name="inspect", tool_call_id="call-2", content="ok"
        )
        with self.assertRaisesRegex(
            ScopedContextError, "APPEND_ONLY_MESSAGE_CONFLICT"
        ):
            strict_append_messages([first], [changed])

    def test_missing_id_and_corrupt_existing_duplicates_fail_closed(self) -> None:
        with self.assertRaisesRegex(ScopedContextError, "MESSAGE_ID_REQUIRED"):
            strict_append_messages([], [HumanMessage(content="no id")])

        duplicate = HumanMessage(id="same", content="same")
        with self.assertRaisesRegex(
            ScopedContextError, "APPEND_ONLY_STATE_CORRUPT"
        ):
            strict_append_messages([duplicate, duplicate], [])

    def test_projection_excludes_all_non_provider_telemetry(self) -> None:
        message = AIMessage(
            id="assistant-telemetry",
            content="result",
            name="worker",
            usage_metadata=_usage(20),
            response_metadata={
                "latency": 2.4,
                "timestamp": "now",
                "prompt_cache_hit_tokens": 100,
            },
            additional_kwargs={
                "cache_telemetry": {"hit": True},
                "trace_id": "trace-1",
            },
        )
        self.assertEqual(
            provider_visible_payload(message),
            {"type": "ai", "content": "result", "name": "worker"},
        )


class ParallelToolOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assistant = AIMessage(
            id="assistant-tools",
            content="",
            tool_calls=[
                {
                    "id": "call-second-result",
                    "name": "second",
                    "args": {"value": 2},
                    "type": "tool_call",
                },
                {
                    "id": "call-first-result",
                    "name": "first",
                    "args": {"value": 1},
                    "type": "tool_call",
                },
            ],
        )
        self.first_arrival = ToolMessage(
            id="tool-first-arrival",
            content="first finished",
            name="first",
            tool_call_id="call-first-result",
        )
        self.second_arrival = ToolMessage(
            id="tool-second-arrival",
            content="second finished",
            name="second",
            tool_call_id="call-second-result",
        )

    def test_parallel_results_are_sorted_by_assistant_call_order_then_appended(self) -> None:
        ordered = order_parallel_tool_messages(
            self.assistant, [self.first_arrival, self.second_arrival]
        )
        self.assertEqual(
            [message.tool_call_id for message in ordered],
            ["call-second-result", "call-first-result"],
        )

        transcript = append_parallel_tool_messages(
            [self.assistant],
            self.assistant,
            [self.first_arrival, self.second_arrival],
        )
        self.assertEqual(
            [message.id for message in transcript],
            ["assistant-tools", "tool-second-arrival", "tool-first-arrival"],
        )

    def test_parallel_batch_rejects_missing_unknown_and_duplicate_results(self) -> None:
        with self.assertRaisesRegex(
            ScopedContextError, "PARALLEL_TOOL_RESULT_INCOMPLETE"
        ):
            order_parallel_tool_messages(self.assistant, [self.first_arrival])

        unknown = ToolMessage(
            id="unknown-result", content="?", tool_call_id="unknown-call"
        )
        with self.assertRaisesRegex(
            ScopedContextError, "PARALLEL_TOOL_RESULT_UNKNOWN"
        ):
            order_parallel_tool_messages(
                self.assistant, [self.first_arrival, self.second_arrival, unknown]
            )

        duplicate = ToolMessage(
            id="duplicate-result",
            content="duplicate",
            tool_call_id="call-first-result",
        )
        with self.assertRaisesRegex(
            ScopedContextError, "PARALLEL_TOOL_RESULT_DUPLICATE"
        ):
            order_parallel_tool_messages(
                self.assistant,
                [self.first_arrival, duplicate, self.second_arrival],
            )


class StableSeedAndPrefixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.system = SystemMessage(id="system-v1", content="stable phase policy")
        self.seed = HumanMessage(id="seed-v1", content="canonical phase seed")

    def test_fixed_seed_is_idempotent_but_cannot_be_replaced(self) -> None:
        retry = HumanMessage(
            id="seed-v1",
            content="canonical phase seed",
            response_metadata={"latency_ms": 3, "timestamp": "later"},
        )
        self.assertIs(fixed_provider_message(self.seed, retry), self.seed)

        for replacement in (
            HumanMessage(id="seed-v2", content="canonical phase seed"),
            HumanMessage(id="seed-v1", content="rewritten seed"),
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(
                    ScopedContextError, "FIXED_MESSAGE_CHANGED"
                ):
                    fixed_provider_message(self.seed, replacement)

    def test_provider_wire_is_system_then_seed_then_append_only_messages(self) -> None:
        assistant = AIMessage(id="assistant-1", content="working")
        tool = ToolMessage(
            id="tool-1", content="observed", tool_call_id="call-1"
        )
        wire = build_provider_messages({
            "system_message": self.system,
            "seed_message": self.seed,
            "messages": [assistant, tool],
        })
        self.assertEqual(
            [message.id for message in wire],
            ["system-v1", "seed-v1", "assistant-1", "tool-1"],
        )

    def test_visible_history_remains_a_stable_prefix(self) -> None:
        assistant = AIMessage(
            id="assistant-1",
            content="working",
            usage_metadata=_usage(12),
        )
        previous = {
            "system_message": self.system,
            "seed_message": self.seed,
            "messages": [assistant],
        }
        current = {
            "system_message": SystemMessage(
                id="system-v1",
                content="stable phase policy",
                response_metadata={"cache": "hit"},
            ),
            "seed_message": HumanMessage(
                id="seed-v1",
                content="canonical phase seed",
                additional_kwargs={"timestamp": "later"},
            ),
            "messages": [
                AIMessage(
                    id="assistant-1",
                    content="working",
                    usage_metadata=_usage(999),
                ),
                HumanMessage(id="observation-2", content="continue"),
            ],
        }
        self.assertTrue(assert_stable_provider_prefix(previous, current))

        rewritten = dict(current)
        rewritten["seed_message"] = HumanMessage(
            id="seed-v1", content="changed canonical seed"
        )
        with self.assertRaisesRegex(ScopedContextError, "PROVIDER_PREFIX_CHANGED"):
            assert_stable_provider_prefix(previous, rewritten)


class PrivatePhaseStateGraphTests(unittest.TestCase):
    def test_output_schema_hides_local_transcript_from_parent_boundary(self) -> None:
        def local_agent(_state: PhaseContextState) -> dict:
            return {"messages": [AIMessage(id="local-ai-1", content="local work")]}

        def finish(state: PhaseContextState) -> dict:
            wire = build_provider_messages(state)
            return {
                "phase_outcome": {
                    "type": "complete",
                    "provider_message_ids": [message.id for message in wire],
                },
                "transcript_ref": "phase://consultant/example",
            }

        builder = StateGraph(
            PhaseContextState,
            input_schema=PhaseContextInput,
            output_schema=PhaseContextOutput,
        )
        builder.add_node("local_agent", local_agent)
        builder.add_node("finish", finish)
        builder.add_edge(START, "local_agent")
        builder.add_edge("local_agent", "finish")
        builder.add_edge("finish", END)

        result = builder.compile().invoke({
            "phase_id": "consultant:example",
            "system_message": SystemMessage(id="system-v1", content="policy"),
            "seed_message": HumanMessage(id="seed-v1", content="seed"),
        })

        self.assertEqual(
            set(result), {"phase_outcome", "transcript_ref"}
        )
        self.assertEqual(
            result["phase_outcome"]["provider_message_ids"],
            ["system-v1", "seed-v1", "local-ai-1"],
        )
        self.assertNotIn("messages", result)
        self.assertNotIn("system_message", result)
        self.assertNotIn("seed_message", result)


if __name__ == "__main__":
    unittest.main()

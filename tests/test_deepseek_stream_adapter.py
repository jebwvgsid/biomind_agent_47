import asyncio
import unittest

from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

from agent_core.deepseek_stream import (
    DeepSeekReasoningChatOpenAI,
    deepseek_reasoning_stream_enabled,
    enable_deepseek_reasoning_stream,
)
from agent_core.provider_attempt import ProviderStreamObserver


def _sdk_reasoning_chunk(reasoning_content: str) -> dict:
    raw = {
        "id": "reasoning-probe",
        "choices": [
            {
                "delta": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning_content,
                },
                "finish_reason": None,
                "index": 0,
            }
        ],
        "created": 0,
        "model": "deepseek-v4-flash",
        "object": "chat.completion.chunk",
    }
    return ChatCompletionChunk.model_validate(raw).model_dump()


class DeepSeekStreamAdapterTests(unittest.IsolatedAsyncioTestCase):
    def _model(self) -> DeepSeekReasoningChatOpenAI:
        model = ChatOpenAI(
            model="deepseek-v4-flash",
            api_key="test-key",
            base_url="https://example.invalid",
            streaming=True,
        )
        adapted = enable_deepseek_reasoning_stream(model)
        self.assertIs(adapted, model)
        self.assertTrue(deepseek_reasoning_stream_enabled(adapted))
        return adapted

    async def test_sdk_reasoning_delta_reaches_provider_observer(self):
        model = self._model()
        generation = model._convert_chunk_to_generation_chunk(
            _sdk_reasoning_chunk("reason-step"),
            AIMessageChunk,
            {},
        )

        self.assertIsNotNone(generation)
        self.assertEqual(generation.message.content, "")
        self.assertEqual(
            generation.message.additional_kwargs.get("reasoning_content"),
            "reason-step",
        )

        observer = ProviderStreamObserver("adapter-test", asyncio.get_running_loop())
        observer.on_llm_new_token(generation.text, chunk=generation)
        snapshot = observer.snapshot()
        self.assertEqual(snapshot["total_chunk_count"], 1)
        self.assertEqual(snapshot["reasoning_chunk_count"], 1)
        self.assertEqual(snapshot["reasoning_char_count"], len("reason-step"))
        self.assertIsNotNone(snapshot["last_activity_monotonic"])

    async def test_content_delta_is_unchanged(self):
        model = self._model()
        raw = {
            "id": "content-probe",
            "choices": [
                {
                    "delta": {"role": "assistant", "content": "OK"},
                    "finish_reason": None,
                    "index": 0,
                }
            ],
            "created": 0,
            "model": "deepseek-v4-flash",
            "object": "chat.completion.chunk",
        }
        generation = model._convert_chunk_to_generation_chunk(
            ChatCompletionChunk.model_validate(raw).model_dump(),
            AIMessageChunk,
            {},
        )

        self.assertIsNotNone(generation)
        self.assertEqual(generation.message.content, "OK")
        self.assertNotIn("reasoning_content", generation.message.additional_kwargs)

    async def test_non_chat_model_is_not_modified(self):
        fake_model = object()
        self.assertIs(enable_deepseek_reasoning_stream(fake_model), fake_model)
        self.assertFalse(deepseek_reasoning_stream_enabled(fake_model))


if __name__ == "__main__":
    unittest.main()

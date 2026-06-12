import unittest

from agent_core.router import classify_task_mode, normalize_task_mode_decision


class _Response:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self.content = content

    async def ainvoke(self, messages):
        return _Response(self.content)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_classify_task_mode_parses_json(self):
        llm = _FakeLLM(
            '{"task_mode":"chat","retrieval_mode":"kb","confidence":0.91,"reason":"definition"}'
        )

        decision = await classify_task_mode(llm, "TAD 是什么？")

        self.assertEqual(decision["task_mode"], "chat")
        self.assertEqual(decision["retrieval_mode"], "kb")
        self.assertGreater(decision["confidence"], 0.9)

    async def test_classify_task_mode_falls_back_to_managed_on_bad_json(self):
        decision = await classify_task_mode(_FakeLLM("not json"), "帮我跑 scRNA QC")

        self.assertEqual(decision["task_mode"], "managed")
        self.assertEqual(decision["retrieval_mode"], "kb")

    def test_low_confidence_escalates_to_managed(self):
        decision = normalize_task_mode_decision(
            {
                "task_mode": "simple_execute",
                "retrieval_mode": "none",
                "confidence": 0.4,
                "reason": "not sure",
            }
        )

        self.assertEqual(decision["task_mode"], "managed")
        self.assertEqual(decision["retrieval_mode"], "kb")

    def test_lightweight_modes_force_no_retrieval(self):
        decision = normalize_task_mode_decision(
            {
                "task_mode": "readonly",
                "retrieval_mode": "kb",
                "confidence": 0.9,
                "reason": "inspect only",
            }
        )

        self.assertEqual(decision["task_mode"], "readonly")
        self.assertEqual(decision["retrieval_mode"], "none")


if __name__ == "__main__":
    unittest.main()

import unittest

from agent_core.llm_safety import (
    deterministic_failure_solution,
    is_llm_input_rejected,
    llm_failure_summary,
)


class LlmSafetyTests(unittest.TestCase):
    def test_detects_provider_input_rejection(self):
        exc = RuntimeError("Error code: 400 - data_inspection_failed: Input data may contain inappropriate content")

        self.assertTrue(is_llm_input_rejected(exc))
        self.assertIn("input_rejected", llm_failure_summary(exc, "generate", "task_a"))
        self.assertIn("<solution>", deterministic_failure_solution(exc, "generate", "task_a"))


if __name__ == "__main__":
    unittest.main()

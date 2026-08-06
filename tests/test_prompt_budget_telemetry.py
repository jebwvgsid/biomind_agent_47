import unittest

from agent_core.context_budget import build_prompt_budget_telemetry


class PromptBudgetTelemetryTests(unittest.TestCase):
    def test_reports_tokens_components_duplicates_and_headroom(self):
        telemetry = build_prompt_budget_telemetry(
            ["same block", "same block", "different"],
            component_chars={"framework_prompt": 10, "history": 20},
            context_limit_tokens=100,
            warning_ratio=0.5,
        )
        self.assertEqual(telemetry["message_count"], 3)
        self.assertGreater(telemetry["estimated_input_tokens"], 0)
        self.assertGreater(telemetry["duplicate_chars"], 0)
        self.assertIn("remaining_headroom_tokens", telemetry)
        self.assertEqual(telemetry["component_chars"]["history"], 20)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from agent_core.recovery import (
    PreActionRepairDomain,
    RecoveryAction,
    RecoveryDecision,
    RecoveryDecisionKind,
    RecoveryPolicy,
)
from prompts.manager import PromptManager


class GeneratePromptExampleTests(unittest.TestCase):
    def test_every_provider_visible_action_example_is_valid_json(self) -> None:
        prompt = PromptManager.get_generate_v2_prompt({
            "semantic_history": {"task_spec": {"goal": "test"}},
            "current_focus": {"current_step": {}},
        })
        examples = re.findall(
            r'<action_json_example name="([^"]+)">\n(.*?)\n</action_json_example>',
            prompt,
            re.DOTALL,
        )

        self.assertEqual(
            [name for name, _ in examples],
            ["invalid_embedded_placeholder", "valid_env_placeholder", "canonical_execute"],
        )
        for name, example in examples:
            with self.subTest(name=name):
                parsed = json.loads(example)
                self.assertEqual(parsed["action_type"], "execute")

    def test_managed_repair_section_has_only_relevant_machine_contract(self) -> None:
        prompt = PromptManager.get_generate_v2_prompt({
            "semantic_history": {"task_spec": {"goal": "repair one action"}},
            "current_focus": {
                "current_step": {
                    "handle": "step.step-1",
                    "inputs": [
                        "input.variant_vcf",
                        "input.reference_fasta",
                        "input.annotation_gtf",
                    ],
                    "outputs": ["output.report"],
                },
                "feedback": {
                    "recovery_action": {"type": "ACTION_FORMAT_REPAIR"},
                    "action_repair": {
                        "repair_mode": "corrective",
                        "repair_stage": "managed_artifact",
                        "previous_candidate": {
                            "action_type": "execute",
                            "env": {"OUT": "{output:report}.tmp"},
                        },
                        "exact_violations": [{
                            "code": "MANAGED_ARTIFACT_PLACEHOLDER_EMBEDDED",
                            "field_path": "env.OUT",
                            "proposed": "{output:report}.tmp",
                        }],
                        "repair_instruction": "Repair the previous generated action.",
                    },
                },
            },
        })

        repair_match = re.search(r"<action_repair>\n(.*?)\n</action_repair>", prompt, re.DOTALL)
        self.assertIsNotNone(repair_match)
        repair = json.loads(repair_match.group(1))
        self.assertEqual(repair["allowed_inputs"], [
            "input.variant_vcf", "input.reference_fasta", "input.annotation_gtf",
        ])
        self.assertEqual(repair["allowed_outputs"], ["output.report"])
        self.assertIn("complete argv/env scalar value", repair["placeholder_rule"])
        self.assertNotIn("action_repair", json.loads(
            re.search(r"<current_focus>\n(.*?)\n</current_focus>", prompt, re.DOTALL).group(1)
        ).get("feedback", {}))




class CorrectiveRecoverySemanticsTests(unittest.TestCase):

    def test_format_repair_is_canonicalized_to_repair_previous_action(self) -> None:
        decision = RecoveryPolicy(max_format_repairs=1).decide(
            pre_action_evidence={
                "valid": False,
                "repairable": True,
                "repair_domain": PreActionRepairDomain.REPRESENTATION.value,
                "violations": ["JSON_DECODE_ERROR"],
            },
            action_id="action-1",
            step_id="step-1",
            attempt_id="attempt-1",
            budgets={"action_format_repair_count": 0},
        )

        self.assertEqual(decision.kind, RecoveryDecisionKind.FORMAT_REPAIR.value)
        self.assertEqual(decision.recommended_action, RecoveryAction.FORMAT_REPAIR.value)
        self.assertEqual(decision.action_type, "repair_previous_action")

        legacy = RecoveryDecision.from_mapping({
            "schema_version": 4,
            "kind": RecoveryDecisionKind.FORMAT_REPAIR.value,
            "recommended_action": RecoveryAction.FORMAT_REPAIR.value,
            "verdict": "repair",
            "action_type": "regenerate_script",
            "reason": "legacy checkpoint",
        })
        self.assertEqual(legacy.action_type, "repair_previous_action")



if __name__ == "__main__":
    unittest.main()

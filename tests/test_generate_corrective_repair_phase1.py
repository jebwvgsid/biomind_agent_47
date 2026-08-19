from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from agent_core.recovery import (
    RecoveryAction,
    RecoveryDecision,
    RecoveryDecisionKind,
    RecoveryPolicy,
)
from bio_agents import BioAnalystAgent
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


class StructuredActionDiagnosticTests(unittest.TestCase):
    def test_incident_malformed_json_preserves_decoder_diagnostic(self) -> None:
        malformed = """```json
{
  "action_type": "execute",
  "action_kind": "required_validation",
  "purpose": "validation",
  "argv": [
    "bash",
    "-lc",
    "echo ready",
    "env": {
      "VCF": "{input:variant_vcf}"
    }
  ]
}
```"""

        result = BioAnalystAgent._extract_structured_action_result(malformed)

        self.assertEqual(result.action, {})
        self.assertEqual(result.error["stage"], "json_decode")
        self.assertEqual(result.error["code"], "JSON_DECODE_ERROR")
        self.assertEqual(result.error["message"], "Expecting ',' delimiter")
        self.assertEqual(result.error["line"], 9)
        self.assertEqual(result.error["column"], 10)
        self.assertIn('"env": {', result.raw_candidate)
        self.assertLessEqual(len(result.raw_candidate), 1202)
        self.assertEqual(BioAnalystAgent._extract_structured_action(malformed), {})

    def test_argv_object_preserves_field_level_schema_diagnostic(self) -> None:
        raw = json.dumps({
            "action_type": "execute",
            "argv": ["bash", "-lc", "echo ready", {"foo": "bar"}],
            "env": {},
        })

        result = BioAnalystAgent._extract_structured_action_result(raw)

        self.assertEqual(result.error["stage"], "action_schema")
        self.assertEqual(result.error["code"], "ACTION_SCHEMA_TYPE_ERROR")
        self.assertEqual(result.error["field_path"], "argv[3]")
        self.assertEqual(result.error["expected"], "string")
        self.assertEqual(result.error["actual"], "object")
        self.assertEqual(result.candidate["argv"][3], {"foo": "bar"})

    def test_env_object_value_preserves_field_level_schema_diagnostic(self) -> None:
        result = BioAnalystAgent._extract_structured_action_result(json.dumps({
            "action_type": "execute",
            "argv": ["tool"],
            "env": {"VCF": {"path": "x"}},
        }))

        self.assertEqual(result.error["field_path"], "env.VCF")
        self.assertEqual(result.error["expected"], "string")
        self.assertEqual(result.error["actual"], "object")

    def test_required_schema_cases_are_distinct(self) -> None:
        cases = [
            ({"action_type": "execute", "argv": "tool"}, "argv", "array"),
            ({"action_type": "execute", "argv": ["tool"], "env": []}, "env", "object"),
            ({"action_type": "execute", "env": {}}, "argv", "non-empty code or argv"),
            ({}, "action_type", "supported action type"),
            ({"argv": ["tool"]}, "action_type", "supported action type"),
            ({"action_type": "invented", "argv": ["tool"]}, "action_type", "supported action type"),
        ]

        for candidate, field_path, expected in cases:
            with self.subTest(candidate=candidate):
                result = BioAnalystAgent._extract_structured_action_result(json.dumps(candidate))
                self.assertEqual(result.error["stage"], "action_schema")
                self.assertEqual(result.error["field_path"], field_path)
                self.assertEqual(result.error["expected"], expected)

    def test_successful_action_shape_is_unchanged(self) -> None:
        raw = json.dumps({
            "action_type": "execute",
            "action_kind": "required_validation",
            "purpose": "validation",
            "argv": ["bash", "-lc", 'tool "$VCF"'],
            "env": {"VCF": "{input:variant_vcf}"},
        })

        result = BioAnalystAgent._extract_structured_action_result(raw)

        self.assertTrue(result.succeeded)
        self.assertEqual(result.error, {})
        self.assertEqual(result.action, BioAnalystAgent._extract_structured_action(raw))
        self.assertEqual(result.action["command_role"], "required_validation")
        self.assertEqual(result.action["env"], {"VCF": "{input:variant_vcf}"})

    def test_later_json_candidate_preserves_existing_fallback_behavior(self) -> None:
        raw = (
            '<action_json>{"action_type":</action_json>\n'
            '<action_json>{"action_type":"execute","argv":["tool"],"env":{}}</action_json>'
        )

        result = BioAnalystAgent._extract_structured_action_result(raw)

        self.assertTrue(result.succeeded)
        self.assertEqual(result.action["type"], "execute")
        self.assertEqual(result.action["argv"], ["tool"])


class CorrectiveRecoverySemanticsTests(unittest.TestCase):
    def test_durable_protocol_diagnosis_keeps_failure_stage_classification(self) -> None:
        cases = [
            ("json", {"stage": "json_decode", "code": "JSON_DECODE_ERROR"}, "json_decode_error"),
            (
                "schema",
                {"stage": "action_schema", "code": "ACTION_SCHEMA_TYPE_ERROR"},
                "action_schema_error",
            ),
        ]
        with tempfile.TemporaryDirectory() as root:
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.data_path = root
            for task_id, protocol_error, expected_type in cases:
                with self.subTest(task_id=task_id):
                    agent._record_action_format_failure(
                        task_id,
                        "step-1",
                        "invalid action",
                        protocol_error,
                    )
                    diagnosis_path = next(
                        (Path(root) / "task_outputs" / task_id / "action_protocol").glob("*.json")
                    )
                    diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
                    self.assertEqual(diagnosis["failure_type"], expected_type)
                    self.assertEqual(diagnosis["protocol_error"], protocol_error)

    def test_format_repair_is_canonicalized_to_repair_previous_action(self) -> None:
        decision = RecoveryPolicy(max_format_repairs=1).decide(
            pre_action_evidence={
                "valid": False,
                "repairable": True,
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

    def test_json_repair_projection_contains_previous_candidate_and_exact_error(self) -> None:
        projection = BioAnalystAgent._action_repair_projection(
            {
                "action_format_repair": {
                    "previous_candidate": '{"argv":["bash","-lc","echo ok","env":{}]}',
                    "protocol_error": {
                        "stage": "json_decode",
                        "code": "JSON_DECODE_ERROR",
                        "message": "Expecting ',' delimiter",
                        "line": 1,
                        "column": 40,
                        "position": 39,
                    },
                },
            },
            {"type": "ACTION_FORMAT_REPAIR"},
            {
                "kind": RecoveryDecisionKind.FORMAT_REPAIR.value,
                "recommended_action": RecoveryAction.FORMAT_REPAIR.value,
            },
        )

        self.assertEqual(projection["repair_mode"], "corrective")
        self.assertEqual(projection["repair_stage"], "json_decode")
        self.assertEqual(projection["exact_error"]["code"], "JSON_DECODE_ERROR")
        self.assertIn('"env":{}', projection["previous_candidate"])
        self.assertIn("instead of regenerating", projection["repair_instruction"])
        self.assertIn("Do not re-plan", projection["repair_instruction"])


if __name__ == "__main__":
    unittest.main()

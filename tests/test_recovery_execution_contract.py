from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.controller_audit import (
    adapt_script_block_to_action,
    parse_executable_script_block,
)
from agent_core.recovery import (
    RecoveryAction,
    RecoveryDecision,
    RecoveryExecutionContractError,
    RecoveryExecutionEnvelope,
    RecoveryPolicy,
    RecoveryResultContract,
    RecoveryStateStore,
    build_recovery_execution_envelope,
)


class RecoveryExecutionEnvelopeTests(unittest.TestCase):
    def _decision(self, action: str) -> RecoveryDecision:
        return RecoveryDecision.from_mapping({
            "schema_version": 5,
            "failure_class": "TOOL_OR_ENV_FAILURE",
            "failure_owner": "RUNTIME",
            "recommended_action": action,
            "reason": "collect bounded evidence",
            "root_cause": "the primary process terminated without enough evidence",
            "evidence": ["exit_code=137"],
            "missing_evidence": ["cgroup memory events"],
            "probe_objective": "capture limits and a bounded input header",
            "confidence": 0.8,
        })

    def _envelope(self, root: Path, action: str = RecoveryAction.RUN_DIAGNOSTIC_PROBE.value):
        decision = self._decision(action)
        return decision, build_recovery_execution_envelope(
            decision,
            recovery_context_id="context-1",
            step_id="step-1",
            task_root=root,
            allowed_input_roots=[str(root / "inputs")],
            forbidden_formal_output_paths=[str(root / "results" / "sample.bam")],
        )

    def test_diagnostic_and_primary_contracts_are_controller_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            diagnostic_decision, diagnostic = self._envelope(root)
            primary_decision, primary = self._envelope(
                root, RecoveryAction.PATCH_SCRIPT.value
            )

        self.assertEqual(diagnostic.execution_purpose, "DIAGNOSTIC")
        self.assertEqual(
            diagnostic.result_contract,
            RecoveryResultContract.DIAGNOSTIC_EVIDENCE.value,
        )
        self.assertFalse(diagnostic.to_dict()["completion_eligible"])
        self.assertTrue(diagnostic.probe_id)
        self.assertEqual(primary.execution_purpose, "PRIMARY")
        self.assertEqual(
            primary.result_contract,
            RecoveryResultContract.FORMAL_OUTPUTS.value,
        )
        self.assertTrue(primary.to_dict()["completion_eligible"])
        self.assertEqual(
            diagnostic.recovery_decision_id,
            diagnostic_decision.decision_id,
        )
        self.assertEqual(primary.recovery_decision_id, primary_decision.decision_id)

    def test_adapter_preserves_ids_hash_and_authority_across_payload_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, envelope = self._envelope(Path(tmp))
            first = adapt_script_block_to_action(
                parse_executable_script_block("<shell>ulimit -a</shell>"),
                execution_envelope=envelope.to_dict(),
                recovery_generated=True,
            )
            repaired = adapt_script_block_to_action(
                parse_executable_script_block("<shell>ulimit -a\nhead -n 4 input.fq</shell>"),
                execution_envelope=envelope.to_dict(),
                recovery_generated=True,
                payload_revision=1,
            )

        for key in (
            "recovery_decision_id", "recovery_context_id", "action_id",
            "probe_id", "execution_mode", "result_contract",
        ):
            self.assertEqual(first[key], repaired[key])
        self.assertEqual(first["purpose"], "diagnostic")
        self.assertEqual(repaired["command_role"], "diagnostic")
        self.assertEqual(repaired["payload_revision"], 1)
        self.assertNotEqual(first["code"], repaired["code"])
        self.assertEqual(
            first["recovery_execution_envelope"]["immutable_hash"],
            repaired["recovery_execution_envelope"]["immutable_hash"],
        )

        # Flat model/action fields are not an authority source.
        repaired.update({
            "purpose": "primary",
            "execution_mode": "EXTERNAL_HANDOFF",
            "result_contract": "FORMAL_OUTPUTS",
        })
        restored = RecoveryExecutionEnvelope.from_mapping(
            repaired["recovery_execution_envelope"]
        )
        self.assertEqual(restored.execution_purpose, "DIAGNOSTIC")
        self.assertEqual(restored.execution_mode, "INLINE")
        self.assertEqual(restored.result_contract, "DIAGNOSTIC_EVIDENCE")

    def test_recovery_adapter_fails_closed_without_envelope(self):
        block = parse_executable_script_block("<shell>ulimit -a</shell>")
        with self.assertRaisesRegex(
            RecoveryExecutionContractError,
            "RECOVERY_EXECUTION_ENVELOPE_MISSING",
        ):
            adapt_script_block_to_action(block, recovery_generated=True)
        legacy = adapt_script_block_to_action(block)
        self.assertNotIn("recovery_execution_envelope", legacy)

    def test_cache_round_trip_rejects_envelope_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision, envelope = self._envelope(root)
            final, outcome = RecoveryPolicy().apply_with_outcome(
                decision, {}, {}
            )
            store = RecoveryStateStore(root, "task-a")
            store.save_llm_result(
                "context-1",
                "recovery_judge",
                raw_response="{}",
                parsed_result=decision.to_dict(),
                semantic_decision=decision.to_dict(),
                policy_outcome=outcome.to_dict(),
                execution_envelope=envelope.to_dict(),
                decision_source="cache",
            )
            cached = store.load_llm_result("context-1", "recovery_judge")
            self.assertEqual(
                cached["execution_envelope"]["immutable_hash"],
                envelope.immutable_hash,
            )
            self.assertEqual(cached["semantic_decision"]["root_cause"], decision.root_cause)
            self.assertEqual(final.root_cause, decision.root_cause)

            cache_path = store.root / "llm_results.json"
            cache_text = cache_path.read_text(encoding="utf-8")
            cache_path.write_text(
                cache_text.replace('"execution_purpose": "DIAGNOSTIC"', '"execution_purpose": "PRIMARY"'),
                encoding="utf-8",
            )
            self.assertEqual(
                store.load_llm_result("context-1", "recovery_judge"),
                {},
            )

    def test_policy_preserves_probe_semantics_when_allowed(self):
        decision = self._decision(RecoveryAction.RUN_DIAGNOSTIC_PROBE.value)
        final, outcome = RecoveryPolicy(max_no_progress=2).apply_with_outcome(
            decision,
            {"meaningful_progress": False, "no_progress_count": 1},
            {},
        )
        self.assertIs(final, decision)
        self.assertEqual(final.root_cause, decision.root_cause)
        self.assertEqual(final.missing_evidence, decision.missing_evidence)
        self.assertEqual(final.probe_objective, decision.probe_objective)
        self.assertEqual(outcome.rewrite_reason, "")


if __name__ == "__main__":
    unittest.main()

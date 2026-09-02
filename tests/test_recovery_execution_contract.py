from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.controller_audit import (
    adapt_script_block_to_action,
    parse_executable_script_block,
)
from agent_core.recovery import (
    DiagnosticEvidenceRecord,
    DiagnosticEvidenceStatus,
    ProbeLifecycleRecord,
    ProbeLifecycleStatus,
    RecoveryAction,
    RecoveryDecision,
    RecoveryExecutionContractError,
    RecoveryExecutionEnvelope,
    RecoveryPolicy,
    RecoveryResultContract,
    RecoveryStateStore,
    build_recovery_execution_envelope,
    collect_diagnostic_scratch_metadata,
    validate_diagnostic_action,
    validate_diagnostic_probe_code,
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

    def test_typed_scratch_allows_bounded_writes_only_inside_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, envelope = self._envelope(root)
            scratch = envelope.scratch_capability
            inside = (
                'mkdir -p "$BIOMIND_RECOVERY_SCRATCH_ROOT"\n'
                'head -n 4 /approved/input.fq '
                '> "$BIOMIND_RECOVERY_SCRATCH_ROOT/small.fq"'
            )
            action = adapt_script_block_to_action(
                parse_executable_script_block(f"<shell>{inside}</shell>"),
                execution_envelope=envelope.to_dict(),
                recovery_generated=True,
            )
            self.assertEqual(validate_diagnostic_action(action), (True, ""))

            outside = f"head -n 4 /approved/input.fq > {root / 'escape.fq'}"
            self.assertFalse(
                validate_diagnostic_probe_code(outside, scratch)[0]
            )
            formal = root / "results" / "sample.bam"
            self.assertFalse(
                validate_diagnostic_probe_code(
                    f"head -n 4 /approved/input.fq > {formal}", scratch
                )[0]
            )
            self.assertFalse(
                validate_diagnostic_probe_code(
                    "tool --threads 8 < /dev/null", scratch
                )[0]
            )
            self.assertFalse(
                validate_diagnostic_probe_code("false || true", scratch)[0]
            )
            self.assertFalse(
                validate_diagnostic_probe_code("false; true", scratch)[0]
            )
            self.assertFalse(
                validate_diagnostic_probe_code(
                    'cat /approved/input.fq > "$BIOMIND_RECOVERY_SCRATCH_ROOT/all.fq"',
                    scratch,
                )[0]
            )

    def test_diagnostic_evidence_is_deduplicated_and_never_completion_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision, envelope = self._envelope(root)
            scratch_root = Path(
                envelope.scratch_capability.scratch_root
            )
            scratch_root.mkdir(parents=True)
            (scratch_root / "limits.txt").write_text(
                "memory.max=2147483648\n", encoding="utf-8"
            )
            execution = {
                "attempt_id": "attempt-1",
                "returncode": 0,
                "signal": None,
                "stdout": "memory.max=2147483648\n",
                "stderr": "",
                "stdout_log": str(root / "stdout.log"),
                "stderr_log": str(root / "stderr.log"),
                "validation": {"passed": True},
            }
            record = DiagnosticEvidenceRecord.from_execution(
                execution,
                envelope,
                scratch_artifact_metadata=(
                    collect_diagnostic_scratch_metadata(
                        envelope.scratch_capability
                    )
                ),
            )
            self.assertEqual(
                record.status, DiagnosticEvidenceStatus.SUCCEEDED.value
            )
            self.assertFalse(record.to_dict()["completion_eligible"])
            self.assertTrue(
                all(
                    not item["adopted"]
                    for item in record.to_dict()["scratch_artifact_metadata"]
                )
            )
            store = RecoveryStateStore(root, "task-a")
            first = store.save_diagnostic_evidence(record)
            second = store.save_diagnostic_evidence(record)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(len(store.load_diagnostic_evidence()), 1)

            store.save_probe_lifecycle(ProbeLifecycleRecord(
                recovery_decision_id=decision.decision_id,
                probe_id=envelope.probe_id or "",
                action_id=envelope.action_id,
                status=ProbeLifecycleStatus.NOT_EXECUTED.value,
                validation_fingerprint="rejected-1",
            ))
            self.assertEqual(len(store.load_diagnostic_evidence()), 1)
            self.assertEqual(
                store.load_probe_lifecycle()[-1]["status"],
                ProbeLifecycleStatus.NOT_EXECUTED.value,
            )

    def test_diagnostic_timeout_and_payload_only_repair_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, envelope = self._envelope(root)
            first = adapt_script_block_to_action(
                parse_executable_script_block("<shell>ulimit -a</shell>"),
                execution_envelope=envelope.to_dict(),
                recovery_generated=True,
            )
            repaired = adapt_script_block_to_action(
                parse_executable_script_block(
                    '<shell>ulimit -a > "$BIOMIND_RECOVERY_SCRATCH_ROOT/limits.txt"</shell>'
                ),
                execution_envelope=first["recovery_execution_envelope"],
                recovery_generated=True,
                payload_revision=1,
            )
            self.assertEqual(first["action_id"], repaired["action_id"])
            self.assertEqual(
                first["recovery_execution_envelope"]["immutable_hash"],
                repaired["recovery_execution_envelope"]["immutable_hash"],
            )
            timed_out = DiagnosticEvidenceRecord.from_execution(
                {"attempt_id": "attempt-timeout", "returncode": 124},
                envelope,
                timed_out=True,
            )
            self.assertEqual(
                timed_out.status, DiagnosticEvidenceStatus.TIMED_OUT.value
            )

            for key, replacement in (
                ("execution_purpose", "PRIMARY"),
                ("execution_mode", "EXTERNAL_HANDOFF"),
                ("result_contract", "FORMAL_OUTPUTS"),
            ):
                mutated = envelope.to_dict()
                mutated[key] = replacement
                with self.assertRaises(RecoveryExecutionContractError):
                    RecoveryExecutionEnvelope.from_mapping(mutated)
            mutated = envelope.to_dict()
            mutated["execution_purpose"] = "PRIMARY"
            with self.assertRaisesRegex(
                RecoveryExecutionContractError,
                "RECOVERY_ENVELOPE_MUTATION_ATTEMPT",
            ):
                adapt_script_block_to_action(
                    parse_executable_script_block("<shell>ulimit -a</shell>"),
                    execution_envelope=mutated,
                    recovery_generated=True,
                    payload_revision=1,
                )


if __name__ == "__main__":
    unittest.main()

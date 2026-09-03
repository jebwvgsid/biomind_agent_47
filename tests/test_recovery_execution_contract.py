from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_core.controller_audit import (
    adapt_script_block_to_action,
    parse_executable_script_block,
)
from agent_core.diagnostics import ProgressTracker, build_progress_evidence
from agent_core.plan_tracker import record_recovery_counter_once
from agent_core.recovery import (
    DiagnosticEvidenceRecord,
    DiagnosticEvidenceStatus,
    ExternalExecutionResult,
    ProbeLifecycleRecord,
    ProbeLifecycleStatus,
    RecoveryAction,
    RecoveryDecision,
    RecoveryExecutionContractError,
    RecoveryExecutionEnvelope,
    RecoveryPolicy,
    RecoveryResultContract,
    RecoverySourceScope,
    RecoveryStateStore,
    build_recovery_execution_envelope,
    collect_diagnostic_scratch_metadata,
    create_external_execution_bundle,
    external_submission_unavailable,
    ingest_external_execution_result,
    finalize_recovery_decision,
    validate_external_execution_bundle,
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
        decision = self._finalize(self._decision(action), "step-1")
        return decision, build_recovery_execution_envelope(
            decision,
            recovery_context_id="context-1",
            step_id="step-1",
            task_root=root,
            allowed_input_roots=[str(root / "inputs")],
            forbidden_formal_output_paths=[str(root / "results" / "sample.bam")],
        )

    def _finalize(self, decision: RecoveryDecision, step_id: str) -> RecoveryDecision:
        return finalize_recovery_decision(
            decision,
            source_scope=RecoverySourceScope(
                task_id="task-a",
                plan_id="plan-a",
                plan_version="plan-v1",
                step_id=step_id,
                failed_action_id=f"action:{step_id}",
                failed_execution_attempt_id=f"attempt:{step_id}",
                source_failure_fingerprint=f"failure:{step_id}",
            ),
            recovery_context_fingerprint=f"context:{step_id}",
            source_node="execute",
            expected_state_generation=1,
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

    def test_exactly_once_counters_and_repeated_preaction_exhaustion(self):
        state = {"task_state": {"budgets": {}}}
        self.assertTrue(record_recovery_counter_once(
            state, "diagnostic_probes_dispatched", "attempt-1"
        ))
        self.assertFalse(record_recovery_counter_once(
            state, "diagnostic_probes_dispatched", "attempt-1"
        ))
        self.assertTrue(record_recovery_counter_once(
            state, "diagnostic_probes_completed", "attempt-1"
        ))
        budgets = state["task_state"]["budgets"]
        self.assertEqual(budgets["diagnostic_probes_dispatched"], 1)
        self.assertEqual(budgets["diagnostic_probes_completed"], 1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, envelope = self._envelope(root)
            store = RecoveryStateStore(root, "task-a")
            first = store.record_preaction_rejection(
                envelope_hash=envelope.immutable_hash,
                validation_fingerprint="invalid-shell",
                action_id=envelope.action_id,
                payload_revision=0,
            )
            replay = store.record_preaction_rejection(
                envelope_hash=envelope.immutable_hash,
                validation_fingerprint="invalid-shell",
                action_id=envelope.action_id,
                payload_revision=0,
            )
            second = store.record_preaction_rejection(
                envelope_hash=envelope.immutable_hash,
                validation_fingerprint="invalid-shell",
                action_id=envelope.action_id,
                payload_revision=1,
            )
            self.assertEqual(first["count"], 1)
            self.assertEqual(replay["count"], 1)
            self.assertFalse(replay["terminal"])
            self.assertEqual(second["count"], 2)
            self.assertTrue(second["terminal"])

    def test_external_handoff_waits_and_resumes_diagnostic_by_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RecoveryStateStore(root, "task-a")
            decision = self._decision(
                RecoveryAction.RUN_DIAGNOSTIC_PROBE.value
            )
            decision.preferred_execution_mode = "EXTERNAL_HANDOFF"
            decision = self._finalize(decision, "step-1")
            envelope = build_recovery_execution_envelope(
                decision,
                recovery_context_id="context-external",
                step_id="step-1",
                task_root=store.root.parent,
                allowed_input_roots=[str(root / "inputs")],
                forbidden_formal_output_paths=[
                    str(root / "results" / "sample.bam")
                ],
            )
            script = (
                "#!/bin/bash\n"
                "#SBATCH --job-name=biomind-probe\n"
                "#SBATCH --cpus-per-task=2\n"
                "set -euo pipefail\n"
                "ulimit -a\n"
            )
            external_action = adapt_script_block_to_action(
                parse_executable_script_block(f"<shell>{script}</shell>"),
                execution_envelope=envelope.to_dict(),
                recovery_generated=True,
            )
            self.assertEqual(
                external_action["execution_mode"], "EXTERNAL_HANDOFF"
            )
            self.assertEqual(
                external_action["result_contract"], "EXECUTION_BUNDLE"
            )
            bundle = create_external_execution_bundle(
                task_root=store.root.parent,
                execution_envelope=envelope,
                script_body=script,
                session_id="session-1",
                task_revision=3,
                plan_revision=7,
                working_directory=root,
                approved_input_bindings=[{
                    "binding_id": "genome",
                    "path": str(root / "inputs" / "genome.fasta"),
                }],
                declared_output_paths=[],
                stdout_pattern="slurm-%j.out",
                stderr_pattern="slurm-%j.err",
            )
            store.save_external_execution_bundle(bundle)
            validated = validate_external_execution_bundle(
                bundle,
                approved_root=store.root.parent,
                expected_envelope=envelope,
            )
            waiting = external_submission_unavailable(
                validated,
                approved_root=store.root.parent,
                reason="sbatch: Operation not permitted",
            )

            self.assertEqual(envelope.execution_mode, "EXTERNAL_HANDOFF")
            self.assertEqual(
                envelope.result_contract,
                RecoveryResultContract.EXECUTION_BUNDLE.value,
            )
            self.assertEqual(waiting["status"], "AWAITING_USER_SUBMISSION")
            self.assertFalse(waiting["task_blocked"])
            self.assertFalse(waiting["task_complete"])
            self.assertEqual(waiting["submit_argv"], ["sbatch", bundle.script_path])
            self.assertNotIn(".bam", waiting["submit_command"])

            result = ExternalExecutionResult(
                session_id="session-1",
                step_id="step-1",
                action_id=envelope.action_id,
                recovery_decision_id=envelope.recovery_decision_id,
                resume_token=bundle.resume_token,
                script_sha256=bundle.script_sha256,
                job_id="12345",
                exit_code=0,
                stdout_ref=str(root / "slurm-12345.out"),
                stderr_ref=str(root / "slurm-12345.err"),
            )
            ingested = store.ingest_external_execution_result(
                envelope.action_id,
                result,
                expected_session_id="session-1",
                expected_step_id="step-1",
            )
            self.assertEqual(ingested["route"], "supervisor")
            self.assertFalse(ingested["completion_eligible"])
            self.assertEqual(
                ingested["diagnostic_evidence"]["status"], "SUCCEEDED"
            )
            self.assertEqual(len(store.load_diagnostic_evidence()), 1)

            stale = ExternalExecutionResult(
                **{
                    **result.to_dict(),
                    "session_id": "old-session",
                }
            )
            with self.assertRaisesRegex(
                RecoveryExecutionContractError,
                "EXTERNAL_RESULT_STALE_IDENTITY",
            ):
                ingest_external_execution_result(
                    bundle,
                    stale,
                    approved_root=store.root.parent,
                    expected_session_id="session-1",
                    expected_step_id="step-1",
                    expected_action_id=envelope.action_id,
                )

    def test_external_primary_requires_formal_validation_and_script_is_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision = self._decision(RecoveryAction.PATCH_SCRIPT.value)
            decision.preferred_execution_mode = "EXTERNAL_HANDOFF"
            decision = self._finalize(decision, "step-primary")
            envelope = build_recovery_execution_envelope(
                decision,
                recovery_context_id="context-primary-external",
                step_id="step-primary",
                task_root=root,
            )
            with self.assertRaisesRegex(
                RecoveryExecutionContractError,
                "EXTERNAL_SCRIPT_INVALID",
            ):
                create_external_execution_bundle(
                    task_root=root,
                    execution_envelope=envelope,
                    script_body="#!/bin/bash\necho missing-sbatch\n",
                    session_id="session-primary",
                    task_revision=1,
                    plan_revision=1,
                    working_directory=root,
                )
            with self.assertRaisesRegex(
                RecoveryExecutionContractError,
                "EXTERNAL_SCRIPT_INVALID",
            ):
                create_external_execution_bundle(
                    task_root=root,
                    execution_envelope=envelope,
                    script_body=(
                        "#!/bin/bash\n#SBATCH --job-name=broken\n"
                        "if true; then\necho missing-fi\n"
                    ),
                    session_id="session-primary",
                    task_revision=1,
                    plan_revision=1,
                    working_directory=root,
                )
            bundle = create_external_execution_bundle(
                task_root=root,
                execution_envelope=envelope,
                script_body=(
                    "#!/bin/bash\n#SBATCH --job-name=primary\n"
                    "set -euo pipefail\necho run-primary\n"
                ),
                session_id="session-primary",
                task_revision=1,
                plan_revision=1,
                working_directory=root,
                declared_output_paths=[str(root / "sample.bam")],
            )
            result = ExternalExecutionResult(
                session_id=bundle.session_id,
                step_id=bundle.step_id,
                action_id=bundle.action_id,
                recovery_decision_id=bundle.recovery_decision_id,
                resume_token=bundle.resume_token,
                script_sha256=bundle.script_sha256,
                job_id="222",
                exit_code=0,
                result_paths=(str(root / "sample.bam"),),
                validated_artifacts=(str(root / "sample.bam"),),
            )
            unvalidated = ingest_external_execution_result(
                bundle,
                result,
                approved_root=root,
                expected_session_id=bundle.session_id,
                expected_step_id=bundle.step_id,
                expected_action_id=bundle.action_id,
            )
            validated = ingest_external_execution_result(
                bundle,
                result,
                approved_root=root,
                expected_session_id=bundle.session_id,
                expected_step_id=bundle.step_id,
                expected_action_id=bundle.action_id,
                formal_artifacts_validated=True,
            )
            self.assertEqual(
                unvalidated["route"], "formal_artifact_validation"
            )
            self.assertFalse(unvalidated["completion_eligible"])
            self.assertEqual(validated["route"], "completion_gate")
            self.assertTrue(validated["completion_eligible"])

    def test_offline_session_1788316042_replay_contract(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "session_1788316042_recovery_replay.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial_bam = root / "partial.bam"
            partial_bam.write_bytes(b"x" * fixture["partial_bam_bytes"])
            decision = RecoveryDecision.from_mapping(
                fixture["diagnostic_decision"]
            )
            decision = self._finalize(decision, fixture["step_id"])
            envelope = build_recovery_execution_envelope(
                decision,
                recovery_context_id=fixture["recovery_context_id"],
                step_id=fixture["step_id"],
                task_root=root,
                allowed_input_roots=[str(root)],
                forbidden_formal_output_paths=[str(partial_bam)],
            )
            action = adapt_script_block_to_action(
                parse_executable_script_block(
                    f"<shell>{fixture['diagnostic_shell']}</shell>"
                ),
                execution_envelope=envelope.to_dict(),
                recovery_generated=True,
            )
            allowed, reason = validate_diagnostic_action(action)
            self.assertTrue(allowed, reason)
            record = DiagnosticEvidenceRecord.from_execution(
                {
                    "attempt_id": "mocked-probe-dispatch-1",
                    "returncode": 0,
                    "stdout": fixture["diagnostic_stdout"],
                    "stderr": "",
                },
                envelope,
            )
            counter_state = {"task_state": {"budgets": {}}}
            for attempt in ("primary-attempt-1", "primary-attempt-2"):
                self.assertTrue(record_recovery_counter_once(
                    counter_state, "primary_execution_dispatches", attempt
                ))
                self.assertTrue(record_recovery_counter_once(
                    counter_state, "primary_execution_failures", attempt
                ))
            for counter in (
                "diagnostic_probes_proposed",
                "diagnostic_probes_validated",
                "diagnostic_probes_dispatched",
                "diagnostic_probes_completed",
            ):
                self.assertTrue(record_recovery_counter_once(
                    counter_state, counter, "mocked-probe-dispatch-1"
                ))
                self.assertFalse(record_recovery_counter_once(
                    counter_state, counter, "mocked-probe-dispatch-1"
                ))
            replay_budgets = counter_state["task_state"]["budgets"]
            self.assertEqual(replay_budgets["primary_execution_dispatches"], 2)
            self.assertEqual(replay_budgets["primary_execution_failures"], 2)
            self.assertEqual(replay_budgets["diagnostic_probes_dispatched"], 1)
            self.assertEqual(action["command_role"], "diagnostic")
            self.assertEqual(record.status, "SUCCEEDED")
            self.assertFalse(record.to_dict()["completion_eligible"])
            self.assertEqual(partial_bam.stat().st_size, 93)
            self.assertEqual(fixture["primary_failures"], [137, 137])
            progress = build_progress_evidence(
                {"task_id": fixture["session_id"], "current_step_id": fixture["step_id"]},
                {
                    "attempt_id": "mocked-probe-dispatch-1",
                    "step_id": fixture["step_id"],
                    "purpose": "diagnostic",
                    "command_role": "diagnostic",
                    "failure_fingerprint": "primary-exit-137",
                    "observed_outputs": [str(partial_bam)],
                    "produced_artifacts": [],
                    "diagnostic_evidence": {
                        **record.to_dict(),
                        "deduplicated": False,
                    },
                },
                ProgressTracker(),
            )
            self.assertTrue(
                progress["raw_filesystem_observation"]["observed"]
            )
            self.assertFalse(progress["task_progress"]["meaningful"])
            self.assertTrue(progress["diagnostic_progress"]["meaningful"])
            self.assertTrue(progress["primary_failure_streak_retained"])

            external_decision = RecoveryDecision.from_mapping(
                fixture["diagnostic_decision"]
            )
            external_decision.preferred_execution_mode = "EXTERNAL_HANDOFF"
            external_decision = self._finalize(external_decision, fixture["step_id"])
            external_envelope = build_recovery_execution_envelope(
                external_decision,
                recovery_context_id=fixture["recovery_context_id"],
                step_id=fixture["step_id"],
                task_root=root,
            )
            external_bundle = create_external_execution_bundle(
                task_root=root,
                execution_envelope=external_envelope,
                script_body=fixture["external_script"],
                session_id=fixture["session_id"],
                task_revision=1,
                plan_revision=1,
                working_directory=root,
            )
            external_wait = external_submission_unavailable(
                external_bundle,
                approved_root=root,
                reason="sbatch: Operation not permitted",
            )
            self.assertEqual(
                external_wait["workflow_status"],
                "AWAITING_USER_SUBMISSION",
            )
            self.assertFalse(external_wait["task_blocked"])
            self.assertEqual(
                external_wait["submit_argv"],
                ["sbatch", external_bundle.script_path],
            )


if __name__ == "__main__":
    unittest.main()

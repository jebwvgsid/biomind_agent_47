import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from agent_core.recovery import (
    ArtifactValidator,
    ErrorUnwrapper,
    ExecutionRecord,
    FailureContext,
    HypothesisManager,
    ProgressDetector,
    RecoveryPolicy,
    RecoveryAction,
    RecoveryDecision,
    RecoveryFailureClass,
    RecoveryFailureOwner,
    RecoveryStateStore,
    RepairExecutor,
    evaluate_recovery_trigger,
    constrain_recovery_decision,
    diagnostic_command_masks_failure,
    parse_recovery_judge_decision,
    parse_recovery_decision,
    validate_diagnostic_probe_code,
)


class RecoveryPrimitiveTests(unittest.TestCase):
    def test_diagnostic_probe_must_be_readonly_and_bounded(self):
        self.assertTrue(validate_diagnostic_probe_code("tail -n 80 logs/build.log")[0])
        self.assertTrue(validate_diagnostic_probe_code("find outputs -maxdepth 3 -type f")[0])
        self.assertFalse(validate_diagnostic_probe_code("touch outputs/probe.txt")[0])
        self.assertFalse(validate_diagnostic_probe_code("find / -maxdepth 3 -name '*.log'")[0])
        self.assertFalse(validate_diagnostic_probe_code("find outputs -type f")[0])

    def test_recovery_judge_v2_is_code_free_and_strict(self):
        payload = {
            "schema_version": 2,
            "failure_class": RecoveryFailureClass.CONTRACT_DRIFT.value,
            "failure_owner": RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
            "failure_type": "manifest_contract_error",
            "error_is_real": False,
            "primary_execution_succeeded": True,
            "evidence_sufficient": True,
            "confidence": 0.97,
            "recommended_action": RecoveryAction.RECONCILE_AND_CONTINUE.value,
            "rerun_required": False,
            "consume_retry_budget": False,
            "user_notification_required": False,
            "reason": "renderer manifest supplies the canonical path",
            "root_cause": "renderer manifest supplies the canonical path",
            "evidence": ["manifest artifact_id matches"],
            "missing_evidence": [],
            "repair_strategy": "reconcile the manifest path",
            "next_action": RecoveryAction.RECONCILE_AND_CONTINUE.value,
            "requires_user_input": False,
        }
        decision = parse_recovery_judge_decision(json.dumps(payload))
        self.assertEqual(decision.recommended_action, RecoveryAction.RECONCILE_AND_CONTINUE.value)
        self.assertEqual(decision.failure_owner, RecoveryFailureOwner.ARTIFACT_CONTRACT.value)
        self.assertEqual(decision.verdict, "continue")
        with self.assertRaisesRegex(ValueError, "may not emit executable code"):
            parse_recovery_judge_decision(json.dumps({**payload, "commands": ["cp old new"]}))

    def test_unbound_legacy_acceptance_with_failed_result_fails_closed(self):
        decision = RecoveryDecision.from_mapping({
            "schema_version": 3,
            "failure_class": RecoveryFailureClass.CONTRACT_DRIFT.value,
            "failure_owner": RecoveryFailureOwner.ARTIFACT_CONTRACT.value,
            "failure_type": "legacy_unbound_reconcile",
            "recommended_action": RecoveryAction.RECONCILE_AND_CONTINUE.value,
            "primary_execution_succeeded": False,
            "error_is_real": True,
            "evidence_sufficient": True,
            "confidence": 1.0,
            "rerun_required": False,
            "consume_retry_budget": False,
            "user_notification_required": False,
            "reason": "legacy payload has no accepted Execute",
            "root_cause": "legacy payload has no accepted Execute",
            "evidence": ["returncode=1"],
        })

        self.assertEqual(decision.kind, "ESCALATE")
        self.assertEqual(decision.disposition, "INTERNAL_PROTOCOL_ERROR")
        self.assertEqual(decision.target_route, "supervisor")

    def test_recovery_decision_accepts_legacy_schema(self):
        decision = parse_recovery_decision(json.dumps({
            "verdict": "repair",
            "root_cause": "script quoting error",
            "confidence": 0.8,
            "action_type": "regenerate_script",
            "evidence": ["shell trace"],
        }))
        self.assertEqual(decision.recommended_action, RecoveryAction.PATCH_SCRIPT.value)
        self.assertEqual(decision.schema_version, 2)

    def test_recovery_judge_cannot_turn_runtime_failure_into_user_question(self):
        judge_decision = RecoveryDecision.from_mapping({
            "schema_version": 2,
            "failure_class": RecoveryFailureClass.MISSING_REQUIRED_INPUT.value,
            "failure_owner": RecoveryFailureOwner.INPUT_DATA.value,
            "recommended_action": RecoveryAction.ASK_USER.value,
            "requires_user_input": True,
            "reason": "ask which runtime to use",
            "evidence": ["LLM inference"],
        })
        bounded = constrain_recovery_decision(judge_decision, {
            "root_cause": "A login shell replaced the verified runtime bindings.",
            "failure_owner": RecoveryFailureOwner.RUNTIME.value,
            "failure_type": "runtime_drift",
            "evidence": ["shell_trace", "runtime_contract"],
            "confidence": 0.96,
            "missing_evidence": [],
            "repair_strategy": "Patch the current script.",
            "next_action": RecoveryAction.PATCH_SCRIPT.value,
            "requires_user_input": False,
            "evidence_sufficient": True,
        })

        self.assertEqual(bounded.failure_owner, RecoveryFailureOwner.RUNTIME.value)
        self.assertEqual(bounded.recommended_action, RecoveryAction.PATCH_SCRIPT.value)
        self.assertFalse(bounded.requires_user_input)

    def test_plan_patch_requires_plan_failure_owner_and_class(self):
        valid = RecoveryDecision.from_mapping({
            "schema_version": 2,
            "failure_class": RecoveryFailureClass.PLAN_ERROR.value,
            "failure_owner": RecoveryFailureOwner.PLAN.value,
            "recommended_action": RecoveryAction.PATCH_PLAN.value,
            "reason": "step dependency is internally contradictory",
            "evidence": ["plan contract"],
        })
        self.assertEqual(valid.failure_owner, RecoveryFailureOwner.PLAN.value)

        with self.assertRaisesRegex(ValueError, "PATCH_PLAN requires"):
            RecoveryDecision.from_mapping({
                "schema_version": 2,
                "failure_class": RecoveryFailureClass.TOOL_OR_ENV_FAILURE.value,
                "failure_owner": RecoveryFailureOwner.RUNTIME.value,
                "recommended_action": RecoveryAction.PATCH_PLAN.value,
                "reason": "tool path missing",
                "evidence": ["stderr"],
            })

    def test_trigger_second_same_failure_without_progress_requires_judge(self):
        evidence = {
            "execution_id": "exec-2",
            "returncode": 1,
            "execution_status": "failed",
            "failure_fingerprint": "same",
            "code_hash": "code-a",
            "command_role": "primary",
        }
        trigger = evaluate_recovery_trigger({}, evidence, recovery_history=[{
            "execution_id": "exec-1",
            "failure_fingerprint": "same",
            "code_hash": "code-a",
        }])
        self.assertTrue(trigger.required)
        self.assertTrue(trigger.judge_required)
        self.assertEqual(trigger.same_failure_count, 2)

    def test_trigger_optional_probe_failure_with_fallback_is_warning(self):
        trigger = evaluate_recovery_trigger({}, {
            "execution_id": "probe",
            "returncode": 127,
            "execution_status": "failed",
            "command_role": "optional_probe",
            "fallback_used": True,
        })
        self.assertFalse(trigger.judge_required)
        self.assertEqual(trigger.deterministic_action, RecoveryAction.PASS_WITH_WARNING.value)

    def test_diagnostic_success_or_failure_returns_to_generate_without_recovery(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                trigger = evaluate_recovery_trigger({}, {
                    "execution_id": f"diagnostic-{returncode}",
                    "returncode": returncode,
                    "execution_status": "succeeded" if returncode == 0 else "failed",
                    "command_role": "diagnostic",
                    "stderr_summary": "tool not found" if returncode else "",
                })

                self.assertFalse(trigger.required)
                self.assertEqual(trigger.reasons, ["diagnostic_returns_to_same_step_generate"])

    def test_primary_command_not_found_requires_recovery_diagnosis_not_plan_patch(self):
        missing = evaluate_recovery_trigger({}, {
            "returncode": 127,
            "execution_status": "failed",
            "command_role": "primary",
            "stderr_summary": "snpEff: command not found",
        })
        ready_runtime = evaluate_recovery_trigger({
            "runtime_audit": [{
                "status": "ready",
                "resolved_runtime_paths": {"RESOLVED_TOOL_JAR": "/verified/tool.jar"},
            }],
        }, {
            "returncode": 127,
            "execution_status": "failed",
            "command_role": "primary",
            "stderr_summary": "snpEff: command not found",
        })

        self.assertTrue(missing.judge_required)
        self.assertTrue(ready_runtime.judge_required)
        self.assertEqual(missing.deterministic_action, "")
        self.assertEqual(ready_runtime.deterministic_action, "")
        self.assertNotEqual(missing.deterministic_action, RecoveryAction.PATCH_PLAN.value)
        self.assertNotEqual(ready_runtime.deterministic_action, RecoveryAction.PATCH_PLAN.value)

    def test_required_artifact_missing_requires_causal_recovery_after_supervisor_pass(self):
        trigger = evaluate_recovery_trigger({
            "supervisor_verdict": "PASS",
            "completion_gate_result": {
                "passed": False,
                "classification": "artifact_missing",
            },
        }, {
            "execution_id": "exec",
            "returncode": 0,
            "execution_status": "succeeded",
            "command_role": "primary",
        })
        self.assertTrue(trigger.judge_required)
        self.assertEqual(trigger.deterministic_action, "")

    def test_changed_code_hash_is_new_failure_occurrence_but_still_requires_diagnosis(self):
        trigger = evaluate_recovery_trigger(
            {},
            {
                "execution_id": "exec-new",
                "returncode": 1,
                "failure_fingerprint": "same-error",
                "code_hash": "new-code",
                "stderr_summary": "tool failed",
            },
            recovery_history=[{
                "failure_fingerprint": "same-error",
                "code_hash": "old-code",
            }],
        )

        self.assertEqual(trigger.same_failure_count, 1)
        self.assertTrue(trigger.judge_required)

    def test_supervisor_script_retry_is_advisory_until_recovery_diagnoses_failure(self):
        trigger = evaluate_recovery_trigger(
            {
                "supervisor_verdict": "RETRY",
                "required_next_action": "SCRIPT_PATCH",
                "evidence_status": "sufficient",
            },
            {"returncode": 1, "stderr_summary": "syntax failure"},
        )

        self.assertTrue(trigger.judge_required)
        self.assertEqual(trigger.deterministic_action, RecoveryAction.PATCH_SCRIPT.value)

    def test_supervisor_retry_after_success_requires_judge(self):
        trigger = evaluate_recovery_trigger(
            {
                "supervisor_verdict": "RETRY",
                "required_next_action": "SCRIPT_PATCH",
                "evidence_status": "sufficient",
            },
            {"returncode": 0, "execution_status": "succeeded"},
        )

        self.assertTrue(trigger.judge_required)
        self.assertTrue(trigger.evidence_conflict)

    def test_generic_missing_path_is_not_assumed_to_be_missing_user_input(self):
        generic = evaluate_recovery_trigger({}, {
            "returncode": 1,
            "stderr_summary": "/wrong/generated/path/snpEff.jar: No such file or directory",
        })
        confirmed = evaluate_recovery_trigger({}, {
            "returncode": 1,
            "stderr_summary": "missing required input: sample_R2.fastq.gz",
            "missing_required_input_confirmed": True,
        })

        self.assertTrue(generic.judge_required)
        self.assertEqual(generic.deterministic_action, "")
        self.assertFalse(confirmed.judge_required)
        self.assertEqual(confirmed.deterministic_action, RecoveryAction.BLOCK_MISSING_INPUT.value)

    def test_recovery_rejects_narrative_content_pattern(self):
        with self.assertRaisesRegex(ValueError, "concrete regex"):
            RecoveryDecision.from_mapping({
                "verdict": "repair",
                "root_cause": "script preview failed",
                "confidence": 0.8,
                "action_type": "regenerate_script",
                "evidence": ["exit=141"],
                "verification_checks": [{
                    "check_type": "content_pattern",
                    "target": "step.sh",
                    "expected": "Conditional block around line 20 must be identified",
                    "required": True,
                }],
            })

    def test_recovery_accepts_concrete_content_pattern(self):
        decision = RecoveryDecision.from_mapping({
            "verdict": "repair",
            "root_cause": "missing option",
            "confidence": 0.9,
            "action_type": "regenerate_script",
            "evidence": ["build invocation omitted option"],
            "verification_checks": [{
                "check_type": "content_pattern",
                "target": "step.sh",
                "pattern": r"snpEff\s+build\s+-gtf22",
                "required": True,
            }],
        })

        self.assertEqual(decision.verification_checks[0]["pattern"], r"snpEff\s+build\s+-gtf22")

    def test_execution_record_persists_action_status_and_purpose(self):
        with tempfile.TemporaryDirectory() as root:
            store = RecoveryStateStore(root, "task-a")
            record = ExecutionRecord.from_mapping({
                "execution_id": "exec-1",
                "action_id": "action-1",
                "step_id": "step-1",
                "returncode": 0,
                "purpose": "record_failure",
                "stdout": "status written",
                "final_code": "echo status written",
                "language": "bash",
                "duration_seconds": 1.25,
                "command_role": "required_validation",
                "manifest_script_hash": "script-sha256",
            })

            store.save_execution_record(record)
            persisted = store.load_execution_records()
            self.assertEqual(persisted[0]["action_id"], "action-1")
            self.assertEqual(persisted[0]["execution_status"], "succeeded")
            self.assertEqual(persisted[0]["purpose"], "record_failure")
            self.assertEqual(persisted[0]["command_role"], "required_validation")
            self.assertEqual(persisted[0]["duration_seconds"], 1.25)
            self.assertEqual(persisted[0]["manifest_script_hash"], "script-sha256")
            self.assertTrue(Path(persisted[0]["code_path"]).is_file())
            self.assertTrue(persisted[0]["code_hash"])

    def test_execution_record_preserves_recovery_role(self):
        record = ExecutionRecord.from_mapping({
            "execution_id": "repair-1",
            "returncode": 0,
            "command_role": "recovery",
        })

        self.assertEqual(record.command_role, "recovery")

    def test_execution_record_reuses_existing_full_stream_log(self):
        with tempfile.TemporaryDirectory() as root:
            full_log = Path(root) / "full.stdout.log"
            full_log.write_text("complete stream", encoding="utf-8")
            store = RecoveryStateStore(root, "task-a")
            record = ExecutionRecord.from_mapping({
                "execution_id": "exec-full-log",
                "returncode": 0,
                "stdout": "bounded projection",
                "stdout_log": str(full_log),
            })

            store.save_execution_record(record)

            self.assertEqual(full_log.read_text(encoding="utf-8"), "complete stream")
            self.assertEqual(record.stdout_log, str(full_log))

    def test_execution_code_hash_uses_actual_approved_code_before_redaction(self):
        raw_code = "tool --api-key=do-not-persist --version"
        record = ExecutionRecord.from_mapping({
            "execution_id": "exec-secret",
            "returncode": 1,
            "code": raw_code,
        })

        self.assertEqual(record.code_hash, hashlib.sha256(raw_code.encode()).hexdigest())
        self.assertNotIn("do-not-persist", record.code)

    def test_unwraps_conda_script_and_inner_log_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "logs" / "build.log"
            log.parent.mkdir()
            log.write_text("Unknown option '-gtf22'\n", encoding="utf-8")
            script = root / "build.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "java -jar snpEff.jar build -gtf22 custom_genome > logs/build.log 2>&1\n",
                encoding="utf-8",
            )

            context = FailureContext.from_execution(
                task_id="recovery-test",
                command=f"conda run -n bio_gatk bash {script}",
                cwd=str(root),
                stderr="ERROR conda.cli.main_run:execute: command failed",
                exit_code=1,
            )

            self.assertEqual(context.inner_command.split()[0], "java")
            self.assertIn("build.log", context.related_log_paths[0])
            self.assertIn("Unknown option", context.inner_error)
            self.assertNotIn("conda.cli.main_run", context.inner_error)

    def test_unwraps_bare_python_script_and_slurm_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "run.py"
            script.write_text("subprocess.run(['tool'], check=True)\n", encoding="utf-8")
            slurm_log = root / "slurm-42.err"
            slurm_log.write_text("Traceback: input schema invalid\n", encoding="utf-8")

            unwrapped = ErrorUnwrapper.unwrap(
                "python run.py 2> slurm-42.err",
                cwd=str(root),
            )

            self.assertIn(str(script), unwrapped["script_paths"])
            self.assertIn(str(slurm_log), unwrapped["log_paths"])
            self.assertIn("Traceback", unwrapped["log_evidence"][0]["tail"])

    def test_recovery_decision_requires_structured_json(self):
        decision = parse_recovery_decision(
            json.dumps(
                {
                    "verdict": "repair",
                    "root_cause": "generated command order",
                    "confidence": 0.9,
                    "action_type": "patch_file",
                    "evidence": ["inner log says unknown option"],
                    "commands": [],
                    "patches": [],
                    "verification_checks": [],
                    "rollback_steps": [],
                    "expected_new_evidence": ["build artifact"],
                    "risk_level": "low",
                    "violates_user_constraint": False,
                }
            )
        )

        self.assertEqual(decision.verdict, "repair")
        self.assertEqual(decision.action_type, "patch_file")

    def test_recovery_decision_normalizes_legacy_command_objects(self):
        decision = parse_recovery_decision(json.dumps({
            "verdict": "probe",
            "root_cause": "need one targeted check",
            "confidence": 0.7,
            "action_type": "run_probe",
            "evidence": ["failure log is truncated"],
            "commands": [
                "test -s build.log",
                {"language": "bash", "purpose": "inspect tail", "code": "tail -n 80 build.log"},
            ],
        }))

        self.assertEqual(
            decision.commands,
            ["test -s build.log", "tail -n 80 build.log"],
        )
        self.assertNotIn("{'language'", "\n".join(decision.commands))

    def test_recovery_decision_rejects_invalid_command_objects(self):
        base = {
            "verdict": "probe",
            "root_cause": "need one targeted check",
            "confidence": 0.7,
            "action_type": "run_probe",
            "evidence": ["failure log is truncated"],
        }
        invalid_commands = [
            [{"language": "bash"}],
            [{"code": 42}],
            [""],
            [42],
        ]

        for commands in invalid_commands:
            with self.subTest(commands=commands):
                with self.assertRaises(ValueError):
                    parse_recovery_decision(json.dumps({**base, "commands": commands}))

    def test_recovery_decision_rejects_unparseable_json(self):
        with self.assertRaises(ValueError):
            parse_recovery_decision("commands: [echo test")

    def test_recovery_decision_requires_commands_for_executable_actions(self):
        with self.assertRaisesRegex(ValueError, "requires at least one executable command"):
            parse_recovery_decision(json.dumps({
                "verdict": "probe",
                "root_cause": "need one targeted check",
                "confidence": 0.7,
                "action_type": "inspect_log",
                "evidence": ["failure log is truncated"],
                "commands": [],
            }))

        blocked = parse_recovery_decision(json.dumps({
            "verdict": "blocked",
            "root_cause": "no safe probe remains",
            "confidence": 1.0,
            "action_type": "block_task",
            "evidence": ["retry budget exhausted"],
            "commands": [],
        }))
        self.assertEqual(blocked.commands, [])

    def test_repair_backup_patch_and_rollback_on_later_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "step.sh"
            target.write_text("java build -gtf22\n", encoding="utf-8")
            result = RepairExecutor().apply(
                [{"target": str(target), "operation": "replace", "old": "java build", "new": "java -jar snpEff.jar build"}],
                task_root=root,
                backup_root=root / "recovery" / "backups",
                attempt_id="attempt_001",
            )
            self.assertTrue(result["ok"])
            self.assertIn("java -jar snpEff.jar build", target.read_text(encoding="utf-8"))
            self.assertTrue(Path(result["records"][0]["backup"]).exists())

            failed = RepairExecutor().apply(
                [{"target": str(target), "operation": "replace", "old": "not-present", "new": "bad"}],
                task_root=root,
                backup_root=root / "recovery" / "backups",
                attempt_id="attempt_002",
            )
            self.assertFalse(failed["ok"])
            self.assertIn("java -jar snpEff.jar build", target.read_text(encoding="utf-8"))

    def test_policy_stops_repeated_fingerprint_and_readonly_attempts(self):
        policy = RecoveryPolicy(max_same_fingerprint=2)
        attempts = [
            {"failure_fingerprint_before": "same", "result": "same_failure", "action_type": "inspect_log"},
            {"failure_fingerprint_before": "same", "result": "same_failure", "action_type": "inspect_file"},
        ]
        allowed, reason = policy.check(attempts, "same")
        self.assertFalse(allowed)
        self.assertEqual(reason, "same_failure_fingerprint_limit_reached")

    def test_policy_stops_third_readonly_probe_even_when_labels_differ(self):
        policy = RecoveryPolicy(max_same_fingerprint=5)
        attempts = [
            {"failure_fingerprint_before": "a", "result": "same_failure", "action_type": "run_probe", "commands": ["ls output"]},
            {"failure_fingerprint_before": "b", "result": "same_failure", "action_type": "inspect_log", "commands": ["tail -n 80 build.log"]},
        ]

        allowed, reason = policy.check(attempts, "c", command="grep -i error build.log")

        self.assertFalse(allowed)
        self.assertEqual(reason, "recovery_readonly_budget_exhausted")

    def test_policy_rejects_same_inner_command_before_another_llm_retry(self):
        policy = RecoveryPolicy(max_same_fingerprint=5)
        attempts = [
            {
                "failure_fingerprint_before": "same",
                "result": "same_failure",
                "action_type": "run_probe",
                "commands": ["java -jar snpEff.jar build"],
            }
        ]

        allowed, reason = policy.check(attempts, "same", command="java -jar snpEff.jar build")

        self.assertFalse(allowed)
        self.assertEqual(reason, "same_recovery_command_repeated")

    def test_failure_context_redacts_credentials_but_keeps_error_evidence(self):
        context = FailureContext.from_execution(
            task_id="redaction",
            command="tool --api-key=super-secret",
            stdout="ERROR: failed API_KEY=another-secret",
            stderr="Authorization: Bearer abc.def.ghi",
        )

        serialized = json.dumps(context.llm_view(), ensure_ascii=False)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("another-secret", serialized)
        self.assertNotIn("abc.def.ghi", serialized)
        self.assertIn("ERROR", serialized)

    def test_artifact_validator_supports_records_and_log_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            table = root / "results.tsv"
            table.write_text("gene\tvalue\nA\t1\n", encoding="utf-8")
            log = root / "run.log"
            log.write_text("completed\n", encoding="utf-8")

            result = ArtifactValidator.validate(
                [
                    {"check_type": "record_count", "target": "results.tsv", "expected": {"min": 1}, "required": True},
                    {"check_type": "log_absence", "target": "run.log", "expected": "ERROR", "required": True},
                ],
                cwd=root,
            )

        self.assertTrue(result["passed"], result)

    def test_diagnostic_or_echo_does_not_mask_failure(self):
        command = "ls required.bin || echo '[CHECK_FAILED] required.bin' && tail -n 80 build.log"
        self.assertTrue(diagnostic_command_masks_failure(command))
        with self.assertRaisesRegex(ValueError, "must preserve each failed check"):
            parse_recovery_decision(json.dumps({
                "verdict": "probe",
                "root_cause": "unknown",
                "confidence": 0.4,
                "action_type": "run_probe",
                "evidence": ["build failed"],
                "commands": [command],
            }))

    def test_partial_database_directory_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "data" / "custom"
            database.mkdir(parents=True)
            (database / "genes.gtf").write_text("gene\n", encoding="utf-8")
            (root / "build.log").write_text("ERROR: Database check failed.\n", encoding="utf-8")
            result = ArtifactValidator.validate([{
                "check_type": "artifact_bundle",
                "target": "data/custom",
                "required": True,
                "expected": {
                    "core_files": ["snpEffectPredictor.bin"],
                    "log": "build.log",
                    "forbidden_patterns": ["ERROR", "FATAL"],
                    "load_check": {"actual_exit": 1, "expected_exit": 0},
                },
            }], cwd=root)

        self.assertFalse(result["passed"])
        self.assertIn("core_nonempty=False", result["failed_checks"][0]["detail"])

    def test_large_build_log_is_stored_by_path_not_full_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RecoveryStateStore(tmp, "large-log")
            marker = "LARGE_UNIQUE_PAYLOAD"
            raw = ("normal build line\n" * 5000) + marker + "\n" + ("later build line\n" * 15000) + "ERROR: final build failure\n"
            record = ExecutionRecord.from_mapping({
                "execution_id": "large-build",
                "command": "tool build",
                "returncode": 1,
                "stdout": raw,
                "stderr": "",
            })
            store.save_execution_record(record)
            context = FailureContext.from_execution(
                task_id="large-log",
                command="tool build",
                stdout=raw,
                exit_code=1,
                stdout_log=record.stdout_log,
            )
            path = store.save_context(context)
            persisted = path.read_text(encoding="utf-8")

        self.assertNotIn(marker, persisted)
        self.assertIn(record.stdout_log, persisted)
        self.assertLess(len(persisted), 80_000)

    def test_failure_projection_keeps_proven_invocation_markers(self):
        from agent_core.recovery import failure_log_projection

        projected = failure_log_projection(
            "Command: 'build'\nReading GTF22 data file\nERROR: Database check failed.\n"
        )

        self.assertIn("Command: 'build'", projected["matched_lines"])
        self.assertIn("Reading GTF22 data file", projected["matched_lines"])

    def test_progress_and_artifact_gate_are_deterministic(self):
        self.assertGreater(ProgressDetector.score("patch_file", ["changed command order"], "a", "b", patch_applied=True), 0)
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.md"
            report.write_text("verified", encoding="utf-8")
            result = ArtifactValidator.validate(
                [
                    {"check_type": "file_nonempty", "target": "report.md", "required": True},
                    {"check_type": "content_pattern", "target": "report.md", "expected": "verified", "required": True},
                    {"check_type": "file_nonempty", "target": "missing.tsv", "required": True},
                ],
                cwd=tmp,
            )
            self.assertFalse(result["passed"])
            self.assertEqual(len(result["failed_checks"]), 1)

    def test_recovery_state_store_writes_audit_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RecoveryStateStore(tmp, "task")
            context = FailureContext.from_execution(task_id="task", command="false", stderr="fatal error")
            manager = HypothesisManager()
            manager.upsert("test root cause", "generated_script_bug", 0.8, ["fatal error"])
            store.save_context(context)
            store.save_hypotheses(manager)
            store.append_trace("CAPTURE_FAILURE", {"fingerprint": context.error_fingerprint})
            self.assertTrue((store.root / "failure_context.json").exists())
            self.assertTrue((store.root / "hypotheses.json").exists())
            self.assertTrue((store.root / "recovery_trace.md").exists())

    def test_execution_record_persists_raw_streams_outside_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RecoveryStateStore(tmp, "task")
            record = ExecutionRecord.from_mapping({
                "execution_id": "exec-1",
                "step_id": "step-2",
                "command": "tool input",
                "returncode": 1,
                "stdout": "partial output\n",
                "stderr": "A USER ERROR occurred\n",
            })

            index = store.save_execution_record(record)
            row = json.loads(index.read_text(encoding="utf-8").splitlines()[0])
            stdout_text = Path(row["stdout_log"]).read_text(encoding="utf-8")
            stderr_text = Path(row["stderr_log"]).read_text(encoding="utf-8")

        self.assertTrue(stdout_text.startswith("partial"))
        self.assertIn("A USER ERROR", stderr_text)
        self.assertEqual(record.summary()["exit_code"], 1)
        self.assertNotIn("raw_stdout", record.summary())

    def test_repair_executor_rejects_plan_patch_and_stale_script_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "step.sh"
            target.write_text("echo old\n", encoding="utf-8")
            executor = RepairExecutor()
            wrong_type = executor.apply(
                [{"patch_type": "plan_patch", "target": str(target), "operation": "replace", "old": "old", "new": "new"}],
                task_root=root,
                backup_root=root / "backups",
                attempt_id="wrong-type",
            )
            stale = executor.apply(
                [{
                    "patch_type": "script_patch",
                    "target": str(target),
                    "base_hash": hashlib.sha256(b"different").hexdigest(),
                    "operation": "replace",
                    "old": "old",
                    "new": "new",
                }],
                task_root=root,
                backup_root=root / "backups",
                attempt_id="stale",
            )

        self.assertFalse(wrong_type["ok"])
        self.assertIn("only script_patch", wrong_type["error"])
        self.assertFalse(stale["ok"])
        self.assertIn("base_hash", stale["error"])



if __name__ == "__main__":
    unittest.main()

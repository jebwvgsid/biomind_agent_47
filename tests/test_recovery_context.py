import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent_core.diagnostics import (
    ProgressTracker,
    RecoveryContext,
    build_current_step_context,
    build_progress_evidence,
    build_recovery_context,
)
from agent_core.middleware import error_recovery_middleware
from agent_core.recovery import RecoveryAction, RecoveryStateStore
from prompts.manager import PromptManager


class RecoveryContextTests(unittest.TestCase):
    def _state(self):
        return {
            "task_id": "task-a",
            "task_spec": {
                "goal": "annotate variants",
                "inputs": [{"role": "variant_vcf", "path": "/data/input.vcf"}],
                "output_dir": "/work/out",
                "constraints": ["use the explicit reference"],
            },
            "current_step_id": "step-2",
            "validation_evidence": {
                "validation_id": "validation-1",
                "task_id": "task-a",
                "step_id": "step-2",
                "action_id": "action-1",
                "attempt_id": "attempt-1",
                "status": "INVALID",
                "errors": ["required artifact missing"],
                "warnings": ["optional report unavailable"],
                "returncode_check": {"returncode": 0, "passed": True},
                "manifest_results": [{"id": "manifest", "passed": False}],
                "artifact_results": [{
                    "artifact_id": "annotated_vcf",
                    "path": "/work/out/annotated.vcf",
                    "passed": False,
                }],
                "success_criteria_results": [{
                    "criterion_id": "criterion-output",
                    "passed": False,
                    "reason": "annotated VCF missing",
                }],
                "recoverability_hints": ["repair_current_action"],
                "llm_advisory": {"verdict": "WARN", "reason": "advisory only"},
                "llm_advisory_conflict": True,
            },
            "completion_gate_result": {
                "passed": False,
                "classification": "required_artifact_missing",
                "message": "annotated VCF missing",
                "missing_artifacts": ["/work/out/annotated.vcf"],
            },
            "runtime_audit": [{
                "status": "ready",
                "env": "bio",
                "API_KEY": "must-not-leak",
                "resolved_runtime_paths": {"TOOL": "/opt/bin/tool"},
            }],
        }

    def _plan(self):
        return {
            "task_id": "task-a",
            "plan_version": "plan-v3",
            "steps": [{
                "step_id": "step-2",
                "step_type": "renderer_composite",
                "status": "running",
                "title": "Annotate variants",
                "skill_ids": ["genomics/annotation"],
                "renderer_id": "annotation-renderer",
                "input_contracts": [{"role": "variant_vcf", "required": True}],
                "output_contracts": [{"role": "annotated_vcf", "required": True}],
                "success_criteria": [{"criterion_id": "criterion-output", "required": True}],
                "depends_on": ["step-1"],
                "renderer_execution": {"manifest_path": "", "completed_action_ids": []},
            }],
        }

    def test_recovery_context_is_stable_serializable_and_redacted(self):
        state = self._state()
        evidence = {
            "execution_id": "exec-1",
            "attempt_id": "attempt-1",
            "action_id": "action-1",
            "step_id": "step-2",
            "returncode": 0,
            "execution_status": "succeeded",
            "command_role": "renderer_invocation",
            "declared_outputs": ["/work/out/annotated.vcf"],
            "produced_artifacts": [],
            "stdout_summary": "renderer completed without a manifest",
            "actual_environment": {
                "CONDA_PREFIX": "/opt/conda/envs/bio",
                "DEEPSEEK_API_KEY": "secret-value",
            },
        }
        first = build_recovery_context(state, evidence, plan_state=self._plan())
        second = build_recovery_context(state, evidence, plan_state=self._plan())
        payload = first.to_dict()

        self.assertIsInstance(first, RecoveryContext)
        self.assertEqual(first.context_id, second.context_id)
        self.assertEqual(first.schema_version, "3")
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertNotIn("secret-value", json.dumps(payload))
        self.assertNotIn("must-not-leak", json.dumps(payload))

    def test_context_contains_canonical_step_validation_and_related_attempts(self):
        history = [
            {
                "attempt_id": "unrelated",
                "step_id": "step-1",
                "failure_fingerprint": "other",
                "action_type": "probe",
            },
            {
                "attempt_id": "attempt-0",
                "step_id": "step-2",
                "failure_fingerprint": "same",
                "action_type": "renderer_invocation",
                "command_role": "renderer_invocation",
                "returncode": 0,
                "validation_status": "INVALID",
            },
        ]
        context = build_recovery_context(
            {**self._state(), "failure_fingerprint": "same"},
            {
                "execution_id": "exec-1",
                "attempt_id": "attempt-1",
                "action_id": "action-1",
                "step_id": "step-2",
                "returncode": 0,
                "command_role": "renderer_invocation",
                "failure_fingerprint": "same",
            },
            plan_state=self._plan(),
            recovery_history=history,
        ).to_dict()

        self.assertEqual(context["current_step"]["step_id"], "step-2")
        self.assertEqual(context["current_step"]["renderer_id"], "annotation-renderer")
        self.assertEqual(context["validation"]["status"], "INVALID")
        self.assertEqual(
            context["validation"]["failed_success_criteria"][0]["criterion_id"],
            "criterion-output",
        )
        self.assertEqual([item["attempt_id"] for item in context["recent_attempts"]], ["attempt-0"])
        self.assertEqual(context["completion"]["missing_artifacts"], ["/work/out/annotated.vcf"])
        self.assertNotIn("recovery_decision", context["recent_attempts"][0])

    def test_current_step_context_has_one_canonical_shape(self):
        current = build_current_step_context(
            self._state(),
            self._plan(),
            allowed_actions=["RENDERER_INVOCATION", "BLOCKED"],
        ).to_dict()

        self.assertEqual(current["step_id"], "step-2")
        self.assertEqual(current["step_type"], "renderer_composite")
        self.assertEqual(current["skill_ids"], ["genomics/annotation"])
        self.assertEqual(current["manifest_status"], "absent")
        self.assertEqual(current["allowed_actions"], ["RENDERER_INVOCATION", "BLOCKED"])

    def test_progress_tracks_state_change_not_command_hash(self):
        tracker = ProgressTracker()
        first = tracker.evaluate(
            attempt_id="attempt-1",
            stage_id="step-2",
            action_type="run_probe",
            command="head -20 renderer.py",
            failure_fingerprint="same",
            artifact_snapshot=[],
            manifest_status="absent",
            completed_stage_count=0,
        )
        second = tracker.evaluate(
            attempt_id="attempt-2",
            stage_id="step-2",
            action_type="run_probe",
            command="grep -n manifest renderer.py",
            failure_fingerprint="same",
            previous_failure_fingerprint="same",
            artifact_snapshot=[],
            manifest_status="absent",
            completed_stage_count=0,
        )
        third = tracker.evaluate(
            attempt_id="attempt-3",
            stage_id="step-2",
            action_type="run_probe",
            command="grep -n manifest renderer.py",
            failure_fingerprint="same",
            previous_failure_fingerprint="same",
            artifact_snapshot=[{"path": "/work/out/manifest.json", "size": 200}],
            manifest_status="registered",
            completed_stage_count=1,
        )

        self.assertFalse(first.meaningful_progress)
        self.assertFalse(second.meaningful_progress)
        self.assertIn(second.classification, {"no_progress", "stalled", "exhausted"})
        self.assertTrue(third.meaningful_progress)
        self.assertIn("manifest_registered", {item["event_type"] for item in third.events})

    def test_progress_is_idempotent_for_same_attempt(self):
        tracker = ProgressTracker()
        first = tracker.evaluate(
            attempt_id="attempt-1",
            stage_id="step-2",
            action_type="run_probe",
            command="head -20 renderer.py",
        )
        second = tracker.evaluate(
            attempt_id="attempt-1",
            stage_id="step-2",
            action_type="run_probe",
            command="head -20 renderer.py",
        )

        self.assertEqual(first, second)
        self.assertEqual(len(tracker.decisions), 1)

    def test_probe_purpose_overrides_legacy_renderer_role_for_progress(self):
        tracker = ProgressTracker()
        state = {"task_id": "probe-loop", "current_step_id": "step-1"}
        first = build_progress_evidence(
            state,
            {
                "attempt_id": "attempt-1",
                "step_id": "step-1",
                "purpose": "probe",
                "command_role": "renderer_invocation",
                "command": "head -20 renderer.py",
                "produced_artifacts": [],
            },
            tracker,
        )
        second = build_progress_evidence(
            state,
            {
                "attempt_id": "attempt-2",
                "step_id": "step-1",
                "purpose": "probe",
                "command_role": "renderer_invocation",
                "command": "grep -n manifest renderer.py",
                "produced_artifacts": [],
            },
            tracker,
        )

        self.assertFalse(first["meaningful_progress"])
        self.assertFalse(second["meaningful_progress"])
        self.assertEqual(first["no_progress_count"], 1)
        self.assertEqual(second["no_progress_count"], 2)

    def test_structured_recovery_disables_legacy_recovery_template(self):
        prompt, _ = PromptManager.build_ephemeral_context_result(
            task_spec_context='{"goal":"repair"}',
            history=[HumanMessage(content="<observation>❌ failed</observation>")],
            current_plan="<current_step_context>{}</current_step_context>",
            recovery_context="<recovery_context>{}</recovery_context>",
            current_action_context="Allowed actions: EXECUTE",
        )

        self.assertNotIn("<recovery_mode>", prompt)
        self.assertEqual(prompt.count("<recovery_context>"), 1)

    def test_generate_prompt_has_one_step_validation_and_decision_source(self):
        recovery = {
            "context_id": "ctx-1",
            "validation": {
                "status": "INVALID",
                "deterministic_errors": ["required artifact missing"],
            },
            "progress": {"meaningful_progress": False, "no_progress_count": 1},
            "recovery_decision": {"recommended_action": "PATCH_SCRIPT"},
            "recent_attempts": [{"attempt_id": "attempt-1"}],
        }
        prompt, _ = PromptManager.build_ephemeral_context_result(
            task_spec_context='{"goal":"repair current action"}',
            history=[HumanMessage(content="<observation>old supervisor text</observation>")],
            current_plan=(
                '<current_step_context>{"step_id":"step-2",'
                '"allowed_actions":["EXECUTE"]}</current_step_context>'
            ),
            plan_progress="PlanTracker owns status.",
            recovery_context=(
                "<recovery_context>"
                + json.dumps(recovery, sort_keys=True)
                + "</recovery_context>"
            ),
            supervisor_context="",
            current_action_context="Use CurrentStepContext.",
        )

        self.assertEqual(prompt.count("<current_step_context>"), 1)
        self.assertEqual(prompt.count("<recovery_context>"), 1)
        self.assertEqual(prompt.count('"validation"'), 1)
        self.assertEqual(prompt.count('"recovery_decision"'), 1)
        self.assertNotIn("<recovery_mode>", prompt)

    def test_recovery_context_and_progress_restore_from_checkpoint(self):
        context = build_recovery_context(
            self._state(),
            {
                "execution_id": "exec-1",
                "attempt_id": "attempt-1",
                "step_id": "step-2",
                "returncode": 1,
            },
            plan_state=self._plan(),
        )
        tracker = ProgressTracker()
        tracker.evaluate(
            attempt_id="attempt-1",
            stage_id="step-2",
            action_type="run_probe",
            command="head -20 renderer.py",
            manifest_status="absent",
        )
        with tempfile.TemporaryDirectory() as root:
            store = RecoveryStateStore(root, "task-a")
            store.save_diagnostic_context(context)
            store.save_progress(tracker)
            restored_context = RecoveryContext.from_mapping(store.load_context())
            restored_progress = store.load_progress()

        self.assertEqual(restored_context.context_id, context.context_id)
        self.assertEqual(len(restored_progress.decisions), 1)
        replayed = restored_progress.evaluate(
            attempt_id="attempt-1",
            stage_id="step-2",
            action_type="run_probe",
            command="head -20 renderer.py",
            manifest_status="absent",
        )
        self.assertEqual(replayed.no_progress_streak, 1)
        self.assertEqual(len(restored_progress.decisions), 1)

    def test_generate_repair_result_is_attempt_scoped_and_reusable(self):
        with tempfile.TemporaryDirectory() as root:
            store = RecoveryStateStore(root, "task-a")
            store.save_llm_result(
                "context-1",
                "generate:PATCH_SCRIPT",
                raw_response="<action_json>{}</action_json>",
                parsed_result={},
            )
            same = store.load_llm_result("context-1", "generate:PATCH_SCRIPT")
            other_stage = store.load_llm_result("context-1", "generate:FORMAT_REPAIR")

        self.assertEqual(same["raw_response"], "<action_json>{}</action_json>")
        self.assertEqual(other_stage, {})


class RecoveryJudgeIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_command_not_found_does_not_call_recovery_judge(self):
        class FailIfCalledLLM:
            async def ainvoke(self, _messages):
                raise AssertionError("deterministic failure must not invoke RecoveryJudge")

        async def execute(_state):
            return {
                "messages": [HumanMessage(content="<observation>failed</observation>")],
                "execution_evidence": {
                    "execution_id": "exec-127",
                    "attempt_id": "attempt-127",
                    "action_id": "action-127",
                    "step_id": "step-1",
                    "returncode": 127,
                    "execution_status": "failed",
                    "command_role": "primary",
                    "stderr_summary": "tool: command not found",
                },
            }

        with tempfile.TemporaryDirectory() as root:
            wrapped = error_recovery_middleware(
                FailIfCalledLLM(),
                data_path=root,
            )(execute)
            result = await wrapped({
                "task_id": "deterministic-127",
                "current_step_id": "step-1",
            })

        self.assertIn(
            result["recovery_decision"]["recommended_action"],
            {"PATCH_SCRIPT", "RUN_DIAGNOSTIC_PROBE", "ESCALATE"},
        )

    async def test_same_attempt_calls_recovery_judge_once_and_reuses_result(self):
        calls = 0

        class FakeLLM:
            async def ainvoke(self, messages):
                nonlocal calls
                calls += 1
                return type("Msg", (), {"content": json.dumps({
                    "schema_version": 2,
                    "failure_class": "AMBIGUOUS",
                    "failure_owner": "ARTIFACT_CONTRACT",
                    "failure_type": "validator_conflict",
                    "error_is_real": True,
                    "primary_execution_succeeded": True,
                    "evidence_sufficient": True,
                    "confidence": 0.8,
                    "recommended_action": RecoveryAction.RECONCILE_AND_CONTINUE.value,
                    "rerun_required": False,
                    "consume_retry_budget": False,
                    "user_notification_required": False,
                    "reason": "validator and artifact contract disagree",
                    "root_cause": "validator and artifact contract disagree",
                    "evidence": ["validation-1"],
                    "missing_evidence": [],
                    "repair_strategy": "reconcile the declared artifact",
                    "next_action": RecoveryAction.RECONCILE_AND_CONTINUE.value,
                    "requires_user_input": False,
                })})()

        async def execute(_state):
            return {
                "messages": [HumanMessage(content="<observation>done</observation>")],
                "execution_evidence": {
                    "execution_id": "exec-1",
                    "attempt_id": "attempt-1",
                    "action_id": "action-1",
                    "step_id": "step-2",
                    "returncode": 0,
                    "execution_status": "succeeded",
                    "command_role": "primary",
                },
            }

        state = {
            "task_id": "judge-once",
            "current_step_id": "step-2",
            "supervisor_verdict": "PASS",
            "completion_gate_result": {
                "passed": False,
                "classification": "scientific_warning",
            },
        }
        with tempfile.TemporaryDirectory() as root:
            wrapped = error_recovery_middleware(FakeLLM(), data_path=root)(execute)
            first = await wrapped(state)
            second = await wrapped(state)
            cache = RecoveryStateStore(root, "judge-once").load_llm_result(
                first["recovery_context"]["context_id"],
                "recovery_judge",
            )

        self.assertEqual(calls, 1)
        self.assertTrue(cache)
        self.assertEqual(
            first["recovery_decision"]["decision_id"],
            second["recovery_decision"]["decision_id"],
        )

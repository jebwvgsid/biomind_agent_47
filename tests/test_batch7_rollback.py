import json
import tempfile
import unittest
from pathlib import Path

from agent_core.completion_gate import CompletionGate
from agent_core.plan_tracker import (
    STATUS_INVALIDATED,
    STATUS_PASSED,
    STATUS_RETRY_PENDING,
    STATUS_WAITING_UPSTREAM,
    apply_recovery_decision,
    advance_plan_after_verified_step,
    get_artifact_producer,
    load_plan_state,
    migrate_plan_state,
    record_provenance_event,
    register_renderer_manifest,
    save_plan_state,
)
from agent_core.recovery import RecoveryDecision, RecoveryDecisionKind, RecoveryPolicy


def _state(root: Path, steps):
    state = {
        "task_id": "task-b7",
        "plan_version": "plan_v7",
        "steps": steps,
    }
    return migrate_plan_state(state, "task-b7", root)[0]


def _produce(state, step_id: str, path: Path, content: str, *, action_id: str):
    action = record_provenance_event(state, "action_bound", {"action": {
        "action_id": action_id,
        "step_id": step_id,
        "type": "execute",
        "command_role": "primary",
    }})
    attempt = record_provenance_event(state, "attempt_started", {"action_id": action["action_id"]})
    path.write_text(content, encoding="utf-8")
    finished = record_provenance_event(state, "execution_finished", {
        "attempt_id": attempt["attempt_id"],
        "execution_record": {
            "execution_id": f"exec-{attempt['attempt_id']}",
            "exit_code": 0,
            "execution_status": "succeeded",
            "command_role": "primary",
            "produced_artifacts": [str(path)],
        },
    })
    record_provenance_event(state, "validation_recorded", {
        "attempt_id": attempt["attempt_id"],
        "validation": {"status": "valid", "validation_id": f"valid-{attempt['attempt_id']}"},
    })
    return attempt, finished["produced_artifact_ids"][0]


def _decision(kind: str, **values):
    payload = {
        "schema_version": 4,
        "kind": kind,
        "reason_code": "test_recovery",
        "reason": "structured test recovery",
        "failure_owner": "DEPENDENCY",
        "failure_class": "CONTRACT_DRIFT",
        "evidence": ["validation:test"],
        "plan_id": values.pop("plan_id", ""),
        "plan_version": values.pop("plan_version", "plan_v7"),
        **values,
    }
    return RecoveryDecision.from_mapping(payload)


class UpstreamRollbackTests(unittest.TestCase):
    def _linear(self, root: Path):
        first = root / "first.tsv"
        second = root / "second.tsv"
        state = _state(root, [
            {"step_id": "step-1", "title": "first", "status": STATUS_PASSED,
             "outputs": [str(first)], "artifacts": [{"artifact_id": "first", "path": str(first)}]},
            {"step_id": "step-2", "title": "second", "status": "running", "depends_on": ["step-1"],
             "inputs": [str(first)], "outputs": [str(second)],
             "artifacts": [{"artifact_id": "second", "path": str(second)}]},
            {"step_id": "step-3", "title": "third", "status": STATUS_PASSED, "depends_on": ["step-2"],
             "inputs": [str(second)]},
        ])
        first_attempt, first_id = _produce(state, "step-1", first, "one\n", action_id="produce-first")
        _produce(state, "step-2", second, "two\n", action_id="produce-second")
        return state, first_attempt, first_id

    def test_linear_rollback_moves_real_cursor_and_invalidates_descendants(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state, _, artifact_id = self._linear(Path(tmpdir))
            decision = _decision(
                RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
                plan_id=state["plan_id"],
                failed_step_id="step-2",
                step_id="step-2",
                rollback_to_step_id="step-1",
                first_bad_step_id="step-1",
                invalid_artifact_ids=[artifact_id],
                invalidate_descendants=True,
            )
            result = apply_recovery_decision(state, decision)
            statuses = {step["step_id"]: step["status"] for step in state["steps"]}
            self.assertTrue(result["applied"])
            self.assertEqual(result["current_step_id"], "step-1")
            self.assertEqual(state["current_step_id"], "step-1")
            self.assertEqual(statuses["step-1"], STATUS_RETRY_PENDING)
            self.assertEqual(statuses["step-2"], STATUS_WAITING_UPSTREAM)
            self.assertEqual(statuses["step-3"], STATUS_INVALIDATED)
            self.assertEqual(get_artifact_producer(state, artifact_id)["status"], "invalid")

    def test_branch_rollback_does_not_touch_unrelated_branch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root, [
                {"step_id": "root-a", "title": "a", "status": STATUS_PASSED},
                {"step_id": "child-a", "title": "a2", "status": "running", "depends_on": ["root-a"]},
                {"step_id": "root-b", "title": "b", "status": STATUS_PASSED},
                {"step_id": "child-b", "title": "b2", "status": STATUS_PASSED, "depends_on": ["root-b"]},
            ])
            decision = _decision(
                RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
                plan_id=state["plan_id"], failed_step_id="child-a", step_id="child-a",
                rollback_to_step_id="root-a", first_bad_step_id="root-a",
            )
            apply_recovery_decision(state, decision)
            statuses = {step["step_id"]: step["status"] for step in state["steps"]}
            self.assertEqual(statuses["root-b"], STATUS_PASSED)
            self.assertEqual(statuses["child-b"], STATUS_PASSED)

    def test_non_ancestor_and_plan_version_mismatch_are_rejected_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir), [
                {"step_id": "a", "title": "a", "status": STATUS_PASSED},
                {"step_id": "b", "title": "b", "status": "running"},
            ])
            before = json.dumps(state, sort_keys=True)
            with self.assertRaises(ValueError):
                apply_recovery_decision(state, _decision(
                    RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
                    plan_id=state["plan_id"], failed_step_id="b", step_id="b", rollback_to_step_id="a",
                ))
            self.assertEqual(json.dumps(state, sort_keys=True), before)
            with self.assertRaises(ValueError):
                apply_recovery_decision(state, _decision(
                    RecoveryDecisionKind.RETRY_CURRENT_ACTION.value,
                    plan_id=state["plan_id"], plan_version="plan_v_old", step_id="b",
                ))
            self.assertEqual(json.dumps(state, sort_keys=True), before)

    def test_unknown_artifact_id_is_rejected_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state, _, _ = self._linear(Path(tmpdir))
            before = json.dumps(state, sort_keys=True)
            with self.assertRaisesRegex(ValueError, "outside the current task"):
                apply_recovery_decision(state, _decision(
                    RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
                    plan_id=state["plan_id"], failed_step_id="step-2", step_id="step-2",
                    rollback_to_step_id="step-1", invalid_artifact_ids=["other-task-artifact"],
                ))
            self.assertEqual(json.dumps(state, sort_keys=True), before)

    def test_same_decision_is_applied_once_and_budget_is_not_recharged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state, _, _ = self._linear(Path(tmpdir))
            decision = _decision(
                RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
                plan_id=state["plan_id"], failed_step_id="step-2", step_id="step-2",
                rollback_to_step_id="step-1", first_bad_step_id="step-1",
                idempotency_key="rollback-fixed",
            )
            first = apply_recovery_decision(state, decision)
            second = apply_recovery_decision(state, decision)
            self.assertTrue(first["applied"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(state["task_state"]["budgets"]["upstream_rollback_count"], 1)

    def test_multi_upstream_rolls_back_only_invalid_producer_branch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            left = root / "left.tsv"
            right = root / "right.tsv"
            state = _state(root, [
                {"step_id": "left", "title": "left", "status": STATUS_PASSED,
                 "outputs": [str(left)], "artifacts": [{"artifact_id": "left-output", "path": str(left)}]},
                {"step_id": "right", "title": "right", "status": STATUS_PASSED,
                 "outputs": [str(right)], "artifacts": [{"artifact_id": "right-output", "path": str(right)}]},
                {"step_id": "join", "title": "join", "status": "running",
                 "depends_on": ["left", "right"], "inputs": [str(left), str(right)]},
            ])
            _, left_id = _produce(state, "left", left, "bad\n", action_id="left-action")
            _produce(state, "right", right, "good\n", action_id="right-action")
            left_head = state["provenance_state"]["artifact_heads"][left_id]
            state["provenance_state"]["artifacts"][left_head]["status"] = "invalid"
            dependency = CompletionGate.validate_dependencies(state, "join")
            decision = RecoveryPolicy().decide(
                dependency_evidence=dependency.to_dict(), step_id="join",
                budgets=state["task_state"]["budgets"],
            )
            self.assertEqual(decision.rollback_to_step_id, "left")
            apply_recovery_decision(state, decision)
            statuses = {step["step_id"]: step["status"] for step in state["steps"]}
            self.assertEqual(statuses["left"], STATUS_RETRY_PENDING)
            self.assertEqual(statuses["right"], STATUS_PASSED)
            self.assertEqual(statuses["join"], STATUS_WAITING_UPSTREAM)

    def test_revalidated_upstream_releases_only_ready_descendant_layer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state, _, _ = self._linear(Path(tmpdir))
            decision = _decision(
                RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
                plan_id=state["plan_id"], failed_step_id="step-2", step_id="step-2",
                rollback_to_step_id="step-1", first_bad_step_id="step-1",
            )
            apply_recovery_decision(state, decision)
            self.assertTrue(advance_plan_after_verified_step(state, "new upstream attempt valid"))
            statuses = {step["step_id"]: step["status"] for step in state["steps"]}
            self.assertEqual(statuses["step-2"], "running")
            self.assertEqual(statuses["step-3"], STATUS_INVALIDATED)
            self.assertEqual(state["current_step_id"], "step-2")

    def test_checkpoint_replay_keeps_cursor_and_budget_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state, _, _ = self._linear(root)
            decision = _decision(
                RecoveryDecisionKind.ROLLBACK_UPSTREAM_STEP.value,
                plan_id=state["plan_id"], failed_step_id="step-2", step_id="step-2",
                rollback_to_step_id="step-1", first_bad_step_id="step-1",
                idempotency_key="checkpoint-rollback",
            )
            apply_recovery_decision(state, decision)
            save_plan_state(root, "task-b7", state)
            restored = load_plan_state(root, "task-b7")
            replay = apply_recovery_decision(restored, decision)
            self.assertTrue(replay["duplicate"])
            self.assertEqual(restored["current_step_id"], "step-1")
            self.assertEqual(restored["task_state"]["budgets"]["upstream_rollback_count"], 1)


class ManifestRewindTests(unittest.TestCase):
    def test_rewind_stays_in_plan_step_and_invalidates_stage_descendants(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root, [{
                "step_id": "render", "title": "render", "status": "running",
                "step_type": "renderer_composite", "renderer_id": "renderer.test",
            }])
            manifest_path = root / "manifest.json"
            manifest = {
                "schema_version": 3, "outdir": str(root), "renderer": {"id": "renderer.test"},
                "provenance": {"task_id": "task-b7", "step_id": "render", "producer_action_id": "render-action"},
                "actions": [
                    {"id": "build", "script": "build.sh", "expected_artifact_ids": ["db"]},
                    {"id": "annotate", "script": "annotate.sh", "depends_on": ["build"], "expected_artifact_ids": ["ann"]},
                ],
                "artifacts": [
                    {"artifact_id": "db", "path": "db.bin", "producer": "build.sh"},
                    {"artifact_id": "ann", "path": "ann.vcf", "producer": "annotate.sh"},
                ],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            normalized = register_renderer_manifest(
                state, "render", manifest, manifest_path=manifest_path,
                task_id="task-b7", producer_action_id="render-action",
            )
            stages = {stage["manifest_action_id"]: stage for stage in normalized["stages"]}
            state["steps"][0]["renderer_execution"]["completed_action_ids"] = ["build", "annotate"]
            for stage in state["provenance_state"]["stages"].values():
                stage["status"] = "passed"
            decision = _decision(
                RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value,
                plan_id=state["plan_id"], step_id="render", plan_step_id="render",
                manifest_id=normalized["manifest_id"],
                failed_stage_id=stages["annotate"]["stage_id"],
                rewind_to_stage_id=stages["build"]["stage_id"],
            )
            result = apply_recovery_decision(state, decision)
            stored = state["provenance_state"]["stages"]
            self.assertEqual(state["current_step_id"], "render")
            self.assertEqual(state["current_stage_id"], stages["build"]["stage_id"])
            self.assertEqual(stored[stages["build"]["stage_id"]]["status"], STATUS_RETRY_PENDING)
            self.assertEqual(stored[stages["annotate"]["stage_id"]]["status"], STATUS_INVALIDATED)
            self.assertEqual(state["steps"][0]["renderer_execution"]["completed_action_ids"], [])
            self.assertEqual(result["current_stage_id"], stages["build"]["stage_id"])

    def test_cross_manifest_rewind_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir), [{"step_id": "render", "title": "r", "status": "running"}])
            state["provenance_state"]["manifests"] = {
                "m1": {"manifest_id": "m1", "plan_step_id": "render", "stage_ids": ["s1"]},
                "m2": {"manifest_id": "m2", "plan_step_id": "render", "stage_ids": ["s2"]},
            }
            state["provenance_state"]["stages"] = {
                "s1": {"stage_id": "s1", "manifest_id": "m1", "depends_on_stage_ids": []},
                "s2": {"stage_id": "s2", "manifest_id": "m2", "depends_on_stage_ids": []},
            }
            with self.assertRaises(ValueError):
                apply_recovery_decision(state, _decision(
                    RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value,
                    plan_id=state["plan_id"], step_id="render", plan_step_id="render",
                    manifest_id="m1", failed_stage_id="s2", rewind_to_stage_id="s1",
                ))

    def test_dependency_policy_selects_stage_rewind_not_plan_patch(self):
        dependency = {
            "satisfied": False,
            "step_id": "render",
            "stage_id": "annotate",
            "invalid_dependencies": ["build"],
            "stage_rewind_candidates": [{
                "manifest_id": "manifest-1",
                "failed_stage_id": "annotate",
                "rewind_to_stage_id": "build",
            }],
            "plan_id": "plan-1",
            "plan_version": "plan-v1",
        }
        decision = RecoveryPolicy().decide(
            dependency_evidence=dependency,
            step_id="render",
            budgets={"stage_rewind_count": 0},
        )
        self.assertEqual(decision.kind, RecoveryDecisionKind.REWIND_MANIFEST_STAGE.value)
        self.assertEqual(decision.rewind_to_stage_id, "build")


class DependencyAndCompletionEvidenceTests(unittest.TestCase):
    def test_existing_stale_file_fails_dependency_and_completion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root, [
                {"step_id": "producer", "title": "p", "status": STATUS_PASSED,
                 "outputs": [str(root / "result.tsv")],
                 "artifacts": [{"artifact_id": "result", "path": str(root / "result.tsv")}]},
                {"step_id": "consumer", "title": "c", "status": "running", "depends_on": ["producer"],
                 "inputs": [str(root / "result.tsv")]},
            ])
            _, artifact_id = _produce(state, "producer", root / "result.tsv", "old\n", action_id="produce")
            head = state["provenance_state"]["artifact_heads"][artifact_id]
            state["provenance_state"]["artifacts"][head]["status"] = "stale"
            dependency = CompletionGate.validate_dependencies(state, "consumer")
            self.assertFalse(dependency.satisfied)
            self.assertEqual(dependency.stale_artifacts, [artifact_id])
            step_gate = CompletionGate.validate_step_evidence(state, "producer")
            self.assertFalse(step_gate.completion_satisfied)
            self.assertEqual(step_gate.stale_outputs, [artifact_id])

    def test_task_completion_rejects_invalidated_step_and_old_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "result.tsv"
            state = _state(root, [{
                "step_id": "step-1", "title": "p", "status": STATUS_INVALIDATED,
                "outputs": [str(output)], "artifacts": [{"artifact_id": "result", "path": str(output)}],
            }])
            output.write_text("still exists\n", encoding="utf-8")
            evidence = CompletionGate.validate_task_evidence(state)
            self.assertFalse(evidence.task_complete)
            self.assertEqual(evidence.invalidated_steps, ["step-1"])

    def test_missing_required_producer_requests_plan_patch(self):
        state = _state(Path("/tmp"), [{
            "step_id": "consumer", "title": "c", "status": "running",
            "input_artifact_ids": ["missing-artifact"],
        }])
        dependency = CompletionGate.validate_dependencies(state, "consumer")
        decision = RecoveryPolicy().decide(
            dependency_evidence=dependency.to_dict(),
            step_id="consumer",
            budgets=state["task_state"]["budgets"],
        )
        self.assertEqual(decision.kind, RecoveryDecisionKind.PLAN_PATCH_REQUEST.value)
        self.assertFalse(decision.rollback_to_step_id)

    def test_completion_evidence_is_read_only_and_binds_version_attempt_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "result.tsv"
            state = _state(root, [{
                "step_id": "producer", "title": "p", "status": "running",
                "outputs": [str(output)],
                "artifacts": [{"artifact_id": "result", "path": str(output)}],
            }])
            attempt, artifact_id = _produce(state, "producer", output, "new\n", action_id="produce")
            before = json.dumps(state, sort_keys=True)
            evidence = CompletionGate.validate_step_evidence(state, "producer")
            self.assertTrue(evidence.completion_satisfied)
            self.assertIn(attempt["attempt_id"], evidence.attempt_ids)
            self.assertEqual(len(evidence.artifact_version_ids), 1)
            self.assertEqual(len(evidence.validation_ids), 1)
            self.assertEqual(json.dumps(state, sort_keys=True), before)
            head = state["provenance_state"]["artifact_heads"][artifact_id]
            state["provenance_state"]["artifacts"][head]["status"] = "stale"
            self.assertFalse(CompletionGate.validate_step_evidence(state, "producer").completion_satisfied)

    def test_new_attempt_creates_new_head_and_keeps_old_version_for_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "result.tsv"
            state = _state(root, [{
                "step_id": "producer", "title": "p", "status": "running",
                "outputs": [str(output)],
                "artifacts": [{"artifact_id": "result", "path": str(output)}],
            }])
            _, artifact_id = _produce(state, "producer", output, "old\n", action_id="produce-1")
            old_head = state["provenance_state"]["artifact_heads"][artifact_id]
            state["provenance_state"]["artifacts"][old_head]["status"] = "stale"
            _produce(state, "producer", output, "new\n", action_id="produce-2")
            new_head = state["provenance_state"]["artifact_heads"][artifact_id]
            self.assertNotEqual(new_head, old_head)
            self.assertIn(old_head, state["provenance_state"]["artifacts"])
            self.assertEqual(state["provenance_state"]["artifacts"][old_head]["status"], "stale")
            self.assertEqual(state["provenance_state"]["artifacts"][new_head]["status"], "valid")

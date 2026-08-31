import json
import tempfile
import unittest
from pathlib import Path

from agent_core.plan_tracker import (
    get_artifact_consumers,
    get_artifact_producer,
    get_attempt_artifacts,
    get_manifest_stage_dependencies,
    get_step_dependencies,
    get_step_descendants,
    get_step_input_producers,
    migrate_execution_provenance,
    migrate_plan_state,
    provenance_debug_dump,
    record_provenance_event,
    register_renderer_manifest,
    save_plan_state,
    load_plan_state,
)
from agent_core.recovery import ExecutionRecord, RecoveryStateStore


def _state(root: Path, *, steps=None):
    state = {
        "task_id": "task-a",
        "plan_version": "plan_v3",
        "steps": steps or [
            {
                "step_id": "step-1",
                "title": "produce result",
                "status": "running",
                "outputs": [str(root / "result.tsv")],
                "artifacts": [{
                    "artifact_id": "result",
                    "path": str(root / "result.tsv"),
                    "producer_step_id": "step-1",
                }],
            },
            {
                "step_id": "step-2",
                "title": "consume result",
                "status": "pending",
                "depends_on": ["step-1"],
                "inputs": [str(root / "result.tsv")],
            },
        ],
    }
    return migrate_plan_state(state, "task-a", root)[0]


class StepDagTests(unittest.TestCase):
    def test_queries_follow_declared_dag_not_step_number(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir), steps=[
                {"step_id": "step-9", "title": "root", "status": "running"},
                {"step_id": "step-2", "title": "left", "depends_on": ["step-9"]},
                {"step_id": "step-7", "title": "right", "depends_on": ["step-9"]},
                {"step_id": "step-1", "title": "join", "depends_on": ["step-2", "step-7"]},
            ])
            self.assertEqual(get_step_dependencies(state, "step-1")["step_ids"], ["step-2", "step-7"])
            self.assertEqual(set(get_step_descendants(state, "step-9")["step_ids"]), {"step-2", "step-7", "step-1"})

    def test_cycle_is_reported_structurally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir), steps=[
                {"step_id": "alpha", "title": "alpha", "depends_on": ["beta"]},
                {"step_id": "beta", "title": "beta", "depends_on": ["alpha"]},
            ])
            result = get_step_descendants(state, "alpha")
            self.assertEqual(result["status"], "cycle_error")
            self.assertTrue(result["cycle"])


class ArtifactProvenanceTests(unittest.TestCase):
    def _run_action(self, state, output: Path, content: str, *, role="primary"):
        action = record_provenance_event(state, "action_bound", {
            "task_id": "task-a",
            "action": {
                "action_id": "action-a",
                "step_id": "step-1",
                "type": "execute",
                "command_role": role,
                "action_source": "generate",
                "code": "produce",
            },
            "raw_action": {"type": "execute", "code": "produce"},
        })
        attempt = record_provenance_event(state, "attempt_started", {
            "action_id": action["action_id"],
            "step_id": "step-1",
        })
        output.write_text(content, encoding="utf-8")
        finished = record_provenance_event(state, "execution_finished", {
            "attempt_id": attempt["attempt_id"],
            "execution_record": {
                "execution_id": f"exec-{attempt['attempt_id']}",
                "action_id": action["action_id"],
                "attempt_id": attempt["attempt_id"],
                "step_id": "step-1",
                "command_role": role,
                "exit_code": 0,
                "execution_status": "succeeded",
                "produced_artifacts": [str(output)],
            },
        })
        return action, attempt, finished

    def test_artifact_links_step_action_attempt_and_consumers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root)
            _, attempt, finished = self._run_action(state, root / "result.tsv", "one\n")
            artifact_id = finished["produced_artifact_ids"][0]
            producer = get_artifact_producer(state, artifact_id)
            self.assertEqual(producer["producer_step_id"], "step-1")
            self.assertEqual(producer["producer_action_id"], "action-a")
            self.assertEqual(producer["producer_attempt_id"], attempt["attempt_id"])
            self.assertEqual(get_artifact_consumers(state, artifact_id)["step_ids"], ["step-2"])
            self.assertEqual(get_attempt_artifacts(state, attempt["attempt_id"])["artifact_ids"], [artifact_id])
            self.assertEqual(get_step_input_producers(state, "step-2")["producers"][0]["producer_step_id"], "step-1")

            validation = record_provenance_event(state, "validation_recorded", {
                "attempt_id": attempt["attempt_id"],
                "validation": {"validation_id": "validation-a", "status": "valid"},
            })
            self.assertEqual(validation["validation_id"], "validation-a")
            self.assertEqual(get_artifact_producer(state, artifact_id)["validation_status"], "valid")

    def test_execution_cursor_only_tracks_an_active_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root)
            self.assertEqual(state.get("execution_cursor_step_id", ""), "")
            action = record_provenance_event(state, "action_bound", {
                "action": {"action_id": "cursor-action", "step_id": "step-1", "type": "execute"},
            })
            attempt = record_provenance_event(state, "attempt_started", {"action_id": action["action_id"]})
            self.assertEqual(state["execution_cursor_step_id"], "step-1")

            record_provenance_event(state, "execution_finished", {
                "attempt_id": attempt["attempt_id"],
                "execution_record": {"execution_id": "cursor-exec", "returncode": 0},
            })

            self.assertEqual(state["execution_cursor_step_id"], "")

    def test_same_path_creates_new_version_without_changing_logical_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root)
            _, first_attempt, first = self._run_action(state, root / "result.tsv", "one\n")
            _, second_attempt, second = self._run_action(state, root / "result.tsv", "two\n")
            self.assertEqual(first["produced_artifact_ids"], second["produced_artifact_ids"])
            self.assertNotEqual(first["artifact_version_ids"], second["artifact_version_ids"])
            producer = get_artifact_producer(state, first["produced_artifact_ids"][0])
            self.assertEqual(producer["producer_attempt_id"], second_attempt["attempt_id"])
            self.assertNotEqual(first_attempt["attempt_id"], second_attempt["attempt_id"])

    def test_probe_does_not_register_formal_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root)
            _, attempt, finished = self._run_action(state, root / "result.tsv", "probe\n", role="diagnostic")
            self.assertEqual(finished["produced_artifact_ids"], [])
            self.assertEqual(get_attempt_artifacts(state, attempt["attempt_id"])["artifact_ids"], [])

    def test_preexisting_unchanged_file_is_not_attributed_to_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "result.tsv"
            output.write_text("old\n", encoding="utf-8")
            state = _state(root)
            action = record_provenance_event(state, "action_bound", {
                "action": {"action_id": "action-old", "step_id": "step-1", "type": "execute", "command_role": "primary"},
            })
            attempt = record_provenance_event(state, "attempt_started", {"action_id": action["action_id"]})
            finished = record_provenance_event(state, "execution_finished", {
                "attempt_id": attempt["attempt_id"],
                "execution_record": {
                    "execution_id": "exec-old", "attempt_id": attempt["attempt_id"], "action_id": action["action_id"],
                    "step_id": "step-1", "returncode": 0, "execution_status": "succeeded",
                    "produced_artifacts": [str(output)],
                },
            })
            self.assertEqual(finished["produced_artifact_ids"], [])

    def test_missing_artifact_has_structured_unknown_producer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir))
            artifact_id = state["steps"][0]["output_artifact_ids"][0]
            producer = get_artifact_producer(state, artifact_id)
            self.assertEqual(producer["status"], "expected")
            self.assertEqual(producer["producer_status"], "known")
            missing = get_artifact_producer(state, "artifact-does-not-exist")
            self.assertEqual(missing["producer_status"], "missing")


class RendererStageTests(unittest.TestCase):
    def test_manifest_actions_become_stage_dag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root, steps=[{
                "step_id": "step-1",
                "title": "renderer workflow",
                "step_type": "renderer_composite",
                "renderer_id": "renderer.test",
                "status": "running",
            }])
            manifest_path = root / "manifest.json"
            manifest = {
                "schema_version": 3,
                "outdir": str(root),
                "renderer": {"id": "renderer.test"},
                "provenance": {"task_id": "task-a", "step_id": "step-1", "producer_action_id": "render-a"},
                "actions": [
                    {"id": "build", "script": "build.sh", "expected_artifact_ids": ["db"]},
                    {"id": "annotate", "script": "annotate.sh", "depends_on": ["build"], "expected_artifact_ids": ["vcf"]},
                ],
                "artifacts": [
                    {"artifact_id": "db", "path": "db.bin", "producer": "build.sh"},
                    {"artifact_id": "vcf", "path": "annotated.vcf", "producer": "annotate.sh"},
                ],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            normalized = register_renderer_manifest(
                state, "step-1", manifest, manifest_path=manifest_path,
                task_id="task-a", producer_action_id="render-a",
            )
            self.assertTrue(normalized["manifest_id"])
            stages = normalized["stages"]
            self.assertEqual(len(stages), 2)
            annotate = next(item for item in stages if item["manifest_action_id"] == "annotate")
            deps = get_manifest_stage_dependencies(state, annotate["stage_id"])
            self.assertEqual(len(deps["stage_ids"]), 1)
            self.assertNotEqual(annotate["plan_step_id"], annotate["stage_id"])

    def test_stage_attempt_is_distinct_from_plan_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root, steps=[{
                "step_id": "step-1", "title": "renderer", "step_type": "renderer_composite",
                "renderer_id": "renderer.test", "status": "running",
                "outputs": [str(root / "db.bin")],
                "artifacts": [{"artifact_id": "db", "path": str(root / "db.bin")}],
            }])
            manifest_path = root / "manifest.json"
            manifest = {
                "schema_version": 3, "outdir": str(root), "renderer": {"id": "renderer.test"},
                "provenance": {"task_id": "task-a", "step_id": "step-1", "producer_action_id": "render-a"},
                "actions": [{"id": "build", "script": "build.sh", "expected_artifact_ids": ["db"]}],
                "artifacts": [{"artifact_id": "db", "path": "db.bin", "producer": "build.sh"}],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            normalized = register_renderer_manifest(state, "step-1", manifest, manifest_path=manifest_path, task_id="task-a", producer_action_id="render-a")
            stage_id = normalized["stages"][0]["stage_id"]
            action = record_provenance_event(state, "action_bound", {"action": {
                "action_id": "run-build", "step_id": "step-1", "type": "execute",
                "command_role": "primary", "action_source": "manifest_runner",
                "manifest_id": normalized["manifest_id"], "manifest_action_id": "build",
            }})
            attempt = record_provenance_event(state, "attempt_started", {"action_id": action["action_id"]})
            self.assertEqual(attempt["stage_id"], stage_id)
            self.assertNotEqual(attempt["stage_id"], attempt["step_id"])
            output = root / "db.bin"
            output.write_text("built\n", encoding="utf-8")
            finished = record_provenance_event(state, "execution_finished", {
                "attempt_id": attempt["attempt_id"],
                "execution_record": {
                    "execution_id": f"exec-{attempt['attempt_id']}",
                    "exit_code": 0,
                    "execution_status": "succeeded",
                    "command_role": "primary",
                    "produced_artifacts": [str(output)],
                },
            })
            self.assertEqual(len(finished["produced_artifact_ids"]), 1)
            producer = get_artifact_producer(state, finished["produced_artifact_ids"][0])
            self.assertEqual(producer["producer_stage_id"], stage_id)
            self.assertEqual(producer["producer_attempt_id"], attempt["attempt_id"])
            record_provenance_event(state, "validation_recorded", {
                "attempt_id": attempt["attempt_id"],
                "validation": {"status": "valid", "checks": ["nonempty"]},
            })
            validated = get_artifact_producer(state, finished["produced_artifact_ids"][0])
            self.assertEqual(validated["status"], "valid")

    def test_rebinding_preserves_original_raw_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir))
            first = record_provenance_event(state, "action_bound", {
                "action": {"action_id": "action-a", "step_id": "step-1", "type": "execute", "code": "echo normalized"},
                "raw_action": {"type": "execute", "code": "echo raw"},
            })
            second = record_provenance_event(state, "action_bound", {
                "action": {"action_id": "action-a", "step_id": "step-1", "type": "execute", "code": "echo executed"},
            })
            self.assertEqual(first["raw_action"]["code_summary"], "echo raw")
            self.assertEqual(second["raw_action"]["code_summary"], "echo raw")

    def test_explicit_attempt_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir))
            action = record_provenance_event(state, "action_bound", {"action": {
                "action_id": "action-a", "step_id": "step-1", "type": "execute",
            }})
            first = record_provenance_event(state, "attempt_started", {
                "action_id": action["action_id"], "attempt_id": "attempt-fixed",
            })
            second = record_provenance_event(state, "attempt_started", {
                "action_id": action["action_id"], "attempt_id": "attempt-fixed",
            })
            self.assertEqual(first, second)
            self.assertEqual(len(state["provenance_state"]["attempts"]), 1)
            self.assertEqual(state["provenance_state"]["attempt_counters"]["action-a"], 1)


class ProvenanceMigrationTests(unittest.TestCase):
    def test_legacy_probe_is_not_migrated_as_artifact_producer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir))
            records = [{
                "execution_id": "legacy-action",
                "action_id": "legacy-action",
                "attempt_id": "legacy-attempt",
                "step_id": "step-1",
                "purpose": "probe",
                "command_role": "renderer_invocation",
                "exit_code": 0,
                "execution_status": "succeeded",
                "produced_artifacts": [str(Path(tmpdir) / "result.tsv")],
            }]
            first = migrate_execution_provenance(state, records)
            second = migrate_execution_provenance(state, records)
            self.assertEqual(first["migration_id"], second["migration_id"])
            self.assertEqual(get_attempt_artifacts(state, "legacy-attempt")["artifact_ids"], [])
            self.assertEqual(len(state["provenance_state"]["attempts"]), 1)
            dump = provenance_debug_dump(state)
            self.assertEqual(dump["summary"]["attempt_count"], 1)

    def test_legacy_help_command_overrides_polluted_analysis_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir))
            records = [{
                "execution_id": "legacy-help", "action_id": "legacy-help", "attempt_id": "help-attempt",
                "step_id": "step-1", "purpose": "analysis", "command_role": "renderer_invocation",
                "code": "python renderer.py --help", "exit_code": 0, "execution_status": "succeeded",
            }]
            migrate_execution_provenance(state, records)
            self.assertEqual(state["provenance_state"]["attempts"]["help-attempt"]["command_role"], "diagnostic")

    def test_legacy_formal_output_backfills_producer_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "result.tsv"
            output.write_text("legacy\n", encoding="utf-8")
            state = _state(root)
            records = [{
                "execution_id": "legacy-run", "action_id": "legacy-run", "attempt_id": "legacy-run-attempt",
                "step_id": "step-1", "purpose": "analysis", "command_role": "primary",
                "exit_code": 0, "execution_status": "succeeded", "produced_artifacts": [str(output)],
            }]
            migrate_execution_provenance(state, records)
            artifacts = get_attempt_artifacts(state, "legacy-run-attempt")
            self.assertEqual(len(artifacts["artifact_ids"]), 1)
            producer = get_artifact_producer(state, artifacts["artifact_ids"][0])
            self.assertEqual(producer["producer_attempt_id"], "legacy-run-attempt")

    def test_plan_and_provenance_ids_survive_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root)
            action = record_provenance_event(state, "action_bound", {"action": {
                "action_id": "stable-action", "step_id": "step-1", "type": "execute",
            }})
            attempt = record_provenance_event(state, "attempt_started", {"action_id": action["action_id"]})
            save_plan_state(root, "task-a", state)
            restored = load_plan_state(root, "task-a")
            self.assertEqual(restored["plan_id"], state["plan_id"])
            self.assertIn(attempt["attempt_id"], restored["provenance_state"]["attempts"])

    def test_execution_record_store_upserts_same_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RecoveryStateStore(tmpdir, "task-a")
            first = ExecutionRecord.from_mapping({
                "execution_id": "exec-a", "action_id": "action-a", "attempt_id": "attempt-a",
                "step_id": "step-1", "returncode": 1,
            })
            second = ExecutionRecord.from_mapping({
                "execution_id": "exec-a", "action_id": "action-a", "attempt_id": "attempt-a",
                "step_id": "step-1", "returncode": 0,
            })
            store.save_execution_record(first)
            store.save_execution_record(second)
            rows = store.load_execution_records()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()

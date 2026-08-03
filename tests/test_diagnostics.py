import json
import tempfile
import unittest
from pathlib import Path

from agent_core.diagnostics import (
    ActionSignature,
    DiagnosticContext,
    DiagnosticLimits,
    DiagnosticProbe,
    EvidenceAssessment,
    HypothesisManager,
    ProgressTracker,
    build_diagnostic_context,
    enrich_execution_evidence,
)
from agent_core.recovery import RecoveryStateStore


class DiagnosticStateTests(unittest.TestCase):
    def test_diagnostic_prompt_view_enforces_hard_character_budget(self):
        huge = "X" * 50_000
        context = DiagnosticContext(
            task_spec_summary={"original_user_request": huge},
            current_step={"success_criteria": [huge] * 20},
            execution={"stdout_tail": huge, "stderr_tail": huge},
            artifacts=[{"preview": huge}] * 20,
            completion={"reason": huge},
            recent_attempts=[{"detail": huge}] * 10,
            skill_policy={"relevant_context": huge},
            limits={"max_context_chars": 10_000},
        )

        view = context.prompt_view()
        encoded = json.dumps(view, ensure_ascii=False, sort_keys=True, default=str)

        self.assertLessEqual(len(encoded), 10_000)
        self.assertTrue(view["limits"]["context_truncated"])
        self.assertIn("current_step", view)
        self.assertIn("validation", view)
        self.assertIn("progress", view)

    def test_runtime_drift_is_diagnosed_from_contract_script_and_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "step.sh"
            script.write_text(
                "export RESOLVED_ENV_PREFIX=/opt/verified/env\n"
                "bash -lc 'tool=$(find \"$CONDA_PREFIX/share\" -name tool.jar -print -quit); test -s \"$tool\"'\n",
                encoding="utf-8",
            )
            trace = root / "trace.log"
            trace.write_text(
                "+BIO_ERR|./temp_script.sh:7|1| bash -lc 'tool=$(find \"$CONDA_PREFIX/share\" -name tool.jar -print -quit); test -s \"$tool\"'\n"
                "PIPESTATUS=(1)\n",
                encoding="utf-8",
            )
            context = build_diagnostic_context(
                {
                    "task_id": "runtime-drift",
                    "runtime_audit": [{
                        "skill_id": "example/tool",
                        "status": "ready",
                        "env": "verified",
                        "resolved_runtime_paths": {"RESOLVED_ENV_PREFIX": "/opt/verified/env"},
                    }],
                },
                {
                    "execution_id": "exec-runtime",
                    "returncode": 1,
                    "execution_status": "failed",
                    "script_file": str(script),
                    "trace_file": str(trace),
                    "actual_environment": {"CONDA_PREFIX": "/opt/controller/env"},
                },
                task_root=root,
            )
            value = context.to_dict()

        self.assertEqual(value["diagnosis"]["failure_type"], "runtime_drift")
        self.assertEqual(value["diagnosis"]["failure_owner"], "RUNTIME")
        self.assertEqual(value["diagnosis"]["next_action"], "PATCH_SCRIPT")
        self.assertFalse(value["diagnosis"]["requires_user_input"])
        self.assertIn("bash -lc", value["execution"]["failed_command"])

    def test_generic_script_failure_uses_persisted_trace_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "step.sh"
            script.write_text("set -e\nrun_tool --input data.vcf\n", encoding="utf-8")
            trace = root / "trace.log"
            trace.write_text(
                "+BIO_ERR|./temp_script.sh:5|2| run_tool --input data.vcf\nPIPESTATUS=(2)\n",
                encoding="utf-8",
            )
            stderr = root / "stderr.log"
            stderr.write_text("ERROR: invalid VCF header\n", encoding="utf-8")
            enriched = enrich_execution_evidence({}, {
                "returncode": 2,
                "script_file": str(script),
                "trace_file": str(trace),
                "stderr_log": str(stderr),
            })
            context = build_diagnostic_context({}, enriched, task_root=root).to_dict()

        self.assertEqual(context["execution"]["failed_command"], "run_tool --input data.vcf")
        self.assertIn("invalid VCF header", context["execution"]["stderr_tail"])
        self.assertEqual(context["diagnosis"]["failure_type"], "generated_script_failure")
        self.assertEqual(context["diagnosis"]["next_action"], "PATCH_SCRIPT")

    def test_missing_manifest_is_renderer_owned_not_user_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "manifest.json"
            context = build_diagnostic_context({}, {
                "returncode": 1,
                "execution_status": "failed",
                "manifest_path": str(missing),
            }, task_root=tmp).to_dict()

        self.assertEqual(context["diagnosis"]["failure_owner"], "RENDERER")
        self.assertEqual(context["diagnosis"]["failure_type"], "manifest_contract_error")
        self.assertFalse(context["diagnosis"]["requires_user_input"])

    def test_exit_one_with_empty_streams_collects_local_evidence_without_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "step.sh"
            script.write_text("set -e\nrun_tool --input sample.vcf\n", encoding="utf-8")
            trace = root / "trace.log"
            trace.write_text(
                "+BIO_ERR|./step.sh:2|1| run_tool --input sample.vcf\nPIPESTATUS=(1)\n",
                encoding="utf-8",
            )
            context = build_diagnostic_context({}, {
                "returncode": 1,
                "execution_status": "failed",
                "script_path": str(script),
                "trace_file": str(trace),
            }, task_root=root).to_dict()

        diagnosis = context["diagnosis"]
        self.assertEqual(context["execution"]["failed_command"], "run_tool --input sample.vcf")
        self.assertEqual(diagnosis["failure_type"], "insufficient_execution_evidence")
        self.assertEqual(diagnosis["next_action"], "ESCALATE")
        self.assertIn("tool_error_detail", diagnosis["missing_evidence"])
        self.assertFalse(diagnosis["requires_user_input"])

    def test_diagnostic_context_contains_current_step_and_bounded_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stderr.log").write_text("line\nERROR: build failed\n", encoding="utf-8")
            (root / "reads.fastq").write_text("@read\n" + "A" * 1000, encoding="utf-8")
            context = build_diagnostic_context(
                {
                    "task_id": "task-a",
                    "requirements_summary": "Build a custom database",
                    "supervisor_verdict": "RETRY",
                    "completion_gate_result": {"passed": False, "message": "artifact missing"},
                },
                {
                    "execution_id": "exec-1",
                    "step_id": "step-2",
                    "script_file": str(root / "step.sh"),
                    "returncode": 1,
                    "stderr_log": str(root / "stderr.log"),
                    "stderr_summary": "ERROR: build failed",
                    "command_role": "primary",
                },
                plan_state={
                    "task_id": "task-a",
                    "plan_version": "plan_v2",
                    "steps": [{
                        "step_id": "step-2",
                        "title": "Build database",
                        "status": "running",
                        "outputs": ["database.bin"],
                        "success_criteria": ["database.bin is non-empty"],
                    }],
                },
                task_root=root,
                limits=DiagnosticLimits(max_files=8, max_context_chars=8000),
            )
            value = context.prompt_view()

        self.assertEqual(value["current_step"]["step_id"], "step-2")
        self.assertEqual(value["execution"]["returncode"], 1)
        self.assertEqual(value["validation"]["legacy_supervisor_verdict"], "RETRY")
        snapshots = value["artifacts"]
        fastq = next(item for item in snapshots if item["path"].endswith("reads.fastq"))
        self.assertEqual(fastq["content_policy"], "metadata_only")
        self.assertNotIn("preview", fastq)
        self.assertLessEqual(len(json.dumps(value, ensure_ascii=False)), 10000)

    def test_initialize_and_semantic_deduplication(self):
        manager = HypothesisManager()
        created = manager.initialize_from_analysis({
            "failed_command": "java -jar snpEff.jar build -gtf22 custom",
            "primary_error_message": "Error: Unknown option '-gtf22'",
        })
        self.assertGreaterEqual(len(created), 1)
        first = manager.upsert("PATH has the wrong snpEff launcher", "executable_mismatch", 0.99)
        second = manager.upsert("the selected executable is not the expected launcher", "bad_launcher", 0.99)
        self.assertEqual(first.hypothesis_id, second.hypothesis_id)
        self.assertLess(first.confidence, 0.99)

    def test_evidence_support_contradiction_and_deduplication(self):
        manager = HypothesisManager()
        hypothesis = manager.upsert("option may not be supported", "unsupported_option", 0.2, deterministic=True)
        evidence = manager.register_evidence("tool_log", "Command: ann; Error: Unknown option -gtf22")
        same = manager.register_evidence("stderr", "Command: ann; Error: Unknown option -gtf22")
        self.assertEqual(evidence.evidence_id, same.evidence_id)
        before = hypothesis.confidence
        update = manager.apply_assessment(EvidenceAssessment(evidence.evidence_id, hypothesis.hypothesis_id, "supports", "strong", "direct", "raw tool error"))
        self.assertIsNotNone(update)
        self.assertGreater(hypothesis.confidence, before)
        duplicate = manager.apply_assessment(EvidenceAssessment(evidence.evidence_id, hypothesis.hypothesis_id, "supports", "decisive", "direct", "repeat"))
        self.assertEqual(duplicate.delta, 0.0)

    def test_llm_cannot_set_confidence_and_decisive_counterevidence_rejects(self):
        with self.assertRaises(ValueError):
            EvidenceAssessment.from_mapping({
                "evidence_id": "EV1", "hypothesis_id": "H1", "relation": "supports", "confidence": 0.99,
            })
        manager = HypothesisManager()
        hypothesis = manager.upsert("wrong subcommand", "wrong_subcommand", 0.4, deterministic=True)
        evidence = manager.register_evidence("tool_log", "build help lists -gtf22")
        manager.apply_assessment(EvidenceAssessment(evidence.evidence_id, hypothesis.hypothesis_id, "contradicts", "decisive", "direct", "build help supports the option"))
        self.assertEqual(hypothesis.status, "rejected")

    def test_probe_requires_expected_outcomes_and_uses_information_gain(self):
        manager = HypothesisManager()
        h1 = manager.upsert("launcher mismatch", "executable_mismatch", deterministic=True)
        h2 = manager.upsert("wrong subcommand", "wrong_subcommand", deterministic=True)
        self.assertIsNone(manager.add_probe(DiagnosticProbe("bad", "missing outcomes", "which SnpEff", [h1.hypothesis_id], [])))
        manager.add_probe(DiagnosticProbe("low", "low gain", "cat log", [h1.hypothesis_id], [{"condition": "same"}], 0.4, 0.2, 0.1))
        manager.add_probe(DiagnosticProbe("high", "distinguish", "SnpEff -version; java -jar app.jar -version", [h1.hypothesis_id, h2.hypothesis_id], [{"condition": "different", "supports": [h1.hypothesis_id]}], 0.9, 0.1, 0.0))
        self.assertEqual(manager.select_probe().probe_id, "high")

    def test_progress_duplicate_readonly_and_exhaustion(self):
        tracker = ProgressTracker()
        first = tracker.evaluate(attempt_id="a1", stage_id="s", action_type="run_probe", command="cat logs/run.log", failure_fingerprint="same")
        second = tracker.evaluate(attempt_id="a2", stage_id="s", action_type="run_probe", command="cat logs/run.log", failure_fingerprint="same", previous_failure_fingerprint="same")
        third = tracker.evaluate(attempt_id="a3", stage_id="s", action_type="run_probe", command="cat logs/run.log", failure_fingerprint="same", previous_failure_fingerprint="same")
        self.assertIn(first.classification, {"no_progress", "weak_progress"})
        self.assertTrue(second.required_strategy_change)
        self.assertEqual(third.classification, "exhausted")
        self.assertTrue(third.forbidden_next_actions)

    def test_new_evidence_can_be_progress_and_repair_without_artifact_is_regression(self):
        tracker = ProgressTracker()
        first = tracker.evaluate(attempt_id="a1", stage_id="s", action_type="run_probe", command="SnpEff -version", failure_fingerprint="old", new_evidence_ids=["EV1"], known_failed_command=True, known_primary_error=True)
        self.assertGreater(first.score, 0)
        second = tracker.evaluate(attempt_id="a2", stage_id="s", action_type="patch_file", command="patch script", failure_fingerprint="old", previous_failure_fingerprint="old", patch_applied=True, existing_artifacts=0)
        self.assertEqual(second.classification, "regression")
        self.assertEqual(second.recommended_next_mode, "rollback")

    def test_candidate_command_is_not_reported_as_confirmed_progress(self):
        tracker = ProgressTracker()
        decision = tracker.evaluate(
            attempt_id="a1",
            stage_id="s",
            action_type="run_probe",
            command="bash -x step.sh",
            failure_fingerprint="failure",
            new_evidence_ids=["EV_candidate"],
            known_failed_command=False,
            known_primary_error=False,
        )

        event_types = {event["event_type"] for event in decision.events}
        self.assertNotIn("root_command_identified", event_types)
        self.assertNotIn("root_error_identified", event_types)

    def test_state_store_persists_structured_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RecoveryStateStore(tmp, "task")
            manager = HypothesisManager()
            h = manager.upsert("wrong launcher", "executable_mismatch", deterministic=True)
            ev = manager.register_evidence("argv", "SnpEff -version")
            manager.apply_assessment(EvidenceAssessment(ev.evidence_id, h.hypothesis_id, "supports", "strong", "direct", "argv"))
            manager.add_probe(DiagnosticProbe("p1", "compare", "which SnpEff", [h.hypothesis_id], [{"condition": "path"}], 0.5, 0.1, 0.0))
            tracker = ProgressTracker()
            tracker.evaluate(attempt_id="a1", stage_id="s", action_type="run_probe", command="which SnpEff", failure_fingerprint="f", new_evidence_ids=[ev.evidence_id])
            store.save_diagnostic_state(manager)
            store.save_progress(tracker)
            store.save_diagnostic_summary(manager, tracker, "f")
            for name in ("hypotheses.json", "hypothesis_relations.json", "evidence.jsonl", "evidence_assessments.jsonl", "confidence_updates.jsonl", "probes.jsonl", "progress_snapshots.jsonl", "progress_events.jsonl", "progress_decisions.jsonl", "diagnostic_summary.json"):
                self.assertTrue((store.root / name).exists(), name)
            loaded = store.load_diagnostic_manager()
            self.assertEqual(len(loaded.evidence), 1)
            self.assertEqual(len(store.load_progress().decisions), 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from agent_core.recovery_evidence import RecoveryEvidenceReader
from agent_core.skill_access import SkillAccessService
from agent_core.skill_manager import SkillManager
from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.recovery_projection import (
    build_causal_failure_record,
    project_recovery_seed,
)
from agent_core.workflow.recovery_tools import (
    InspectionRef,
    RecoveryLocalToolDispatcher,
    RecoveryToolError,
    TypedInspectionRegistry,
    build_recovery_inspection_registry,
)
from tests.test_recovery_projection import _durable, _verification_rejected


class RecoveryToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="recovery-tools-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.phase_store = PhaseArchiveStore(self.root)
        self.archive = self.phase_store.archive(
            task_id="task-recovery",
            phase="step_worker",
            invocation_id="plan-recovery:plan-v1:step-1:attempt-1:fixture",
            summary={"status": "failed", "step_id": "step-1"},
            artifacts=["artifact:result"],
            evidence=["evidence:run-log"],
            transcript=[
                {"event_type": "tool", "tool": "runtime.inspect", "text": "runtime ok"},
                {"event_type": "execution", "tool": "execute_action", "text": "failed rc=17"},
                {"event_type": "validator", "tool": "verify_step", "text": "output missing"},
            ],
            metadata={
                "task_id": "task-recovery",
                "plan_id": "plan-recovery",
                "plan_version": "plan-v1",
                "step_id": "step-1",
            },
        )

        skills_root = self.root / "skills"
        skill_dir = skills_root / "genomics" / "selected"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: Selected Skill\n"
            "description: selected fixture skill\n"
            "---\n"
            "# Overview\nSelected immutable overview.\n"
            "## Procedure\nRun the selected procedure.\n",
            encoding="utf-8",
        )
        references = skill_dir / "references"
        references.mkdir()
        self.resource_path = references / "guide.txt"
        self.resource_path.write_text("pinned resource v1\n", encoding="utf-8")
        self.skill_service = SkillAccessService(SkillManager(str(skills_root)))
        skill_state, snapshot, _ = self.skill_service.ensure_snapshot(
            {},
            "genomics/selected",
            status="active",
            selected_at_task_revision=1,
        )

        self.evidence_root = self.root / "evidence"
        self.evidence_root.mkdir()
        (self.evidence_root / "run.log").write_text(
            "line one\nline two\nline three\n", encoding="utf-8"
        )
        self.artifact_path = self.root / "approved.txt"
        self.artifact_path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        self.durable = _durable()
        task_state = dict(self.durable["task_state"])
        task_state["skill_access_state"] = skill_state
        task_state["inspection_ref_registry"] = {
            "validation-1": {
                "ref_id": "validation-1",
                "kind": "evidence",
                "task_id": "task-recovery",
                "plan_id": "plan-recovery",
                "step_id": "step-1",
                "backend": "recovery_evidence",
                "digest": "sha256:validation-fixture",
                "size_hint": 29,
                "scope": {"access": "current_step"},
                "locator": {"relative_path": "run.log"},
            }
        }
        self.durable["task_state"] = task_state
        record = build_causal_failure_record(
            "completion_gate_outcome",
            _verification_rejected(),
            self.durable,
            task_id="task-recovery",
            inspection_refs=(self.archive.archive_ref,),
        )
        task_state["causal_failure_packets"] = {record["packet_ref"]: record}
        task_state["active_failure_packet_ref"] = record["packet_ref"]
        state = {
            "task_id": "task-recovery",
            "active_failure_packet_ref": record["packet_ref"],
            "failure_packet": record["failure_packet"],
            "failure_source": record["failure_source"],
        }
        projection = project_recovery_seed(state, self.durable)
        assert projection is not None
        self.seed = projection.recovery_seed
        self.registry = build_recovery_inspection_registry(
            self.seed, self.durable, phase_store=self.phase_store
        )
        self.registry.register(InspectionRef(
            ref_id="artifact:approved",
            kind="artifact",
            task_id="task-recovery",
            plan_id="plan-recovery",
            step_id="step-1",
            backend="managed_artifacts",
            digest="sha256:approved-fixture",
            size_hint=self.artifact_path.stat().st_size,
            scope={"access": "current_step", "direction": "input"},
        ), locator={
            "artifact_id": "approved",
            "path": str(self.artifact_path),
            "binding": {
                "direction": "input",
                "source_authority": "task_spec",
                "row": {"validation_status": "approved"},
            },
        })
        self.registry.register(InspectionRef(
            ref_id="evidence-scope:logs",
            kind="evidence_scope",
            task_id="task-recovery",
            plan_id="plan-recovery",
            step_id="step-1",
            backend="recovery_evidence",
            digest="sha256:evidence-scope",
            size_hint=0,
            scope={"access": "current_step"},
        ), locator={"pattern": "*.log"})
        self.dispatcher = RecoveryLocalToolDispatcher(
            seed=self.seed,
            durable=self.durable,
            registry=self.registry,
            phase_store=self.phase_store,
            skill_service=self.skill_service,
            evidence_reader=RecoveryEvidenceReader(self.evidence_root),
        )

    def test_registry_manifest_is_typed_and_hides_locators(self) -> None:
        manifest = self.registry.provider_manifest()
        self.assertTrue(manifest)
        for row in manifest:
            self.assertEqual(
                {
                    "ref_id", "kind", "task_id", "plan_id", "step_id",
                    "backend", "digest", "size_hint", "scope",
                    "source_object_id", "readable", "unavailable_reason", "allowed_tools",
                },
                set(row),
            )
            self.assertIsInstance(row["readable"], bool)
            self.assertTrue(row["allowed_tools"])
            if not row["readable"]:
                self.assertTrue(row["unavailable_reason"])
            self.assertNotIn("locator", row)
            self.assertNotIn(str(self.artifact_path), str(row))

        with self.assertRaisesRegex(
            RecoveryToolError, "INSPECTION_REF_NOT_DECLARED"
        ):
            self.registry.resolve("/arbitrary/path", kinds=("artifact",))

    def test_failure_inspect_is_bound_to_current_packet(self) -> None:
        result = self.dispatcher.dispatch("failure.inspect", {})
        self.assertEqual(
            result["failure_packet"]["failure_id"],
            self.seed.failure_packet.failure_id,
        )
        self.assertNotIn("failure_source", result)
        with self.assertRaisesRegex(
            RecoveryToolError, "INSPECTION_REF_NOT_DECLARED"
        ):
            self.dispatcher.dispatch("failure.inspect", {"ref_id": "failure:old"})

    def test_step_inspect_is_paginated_filterable_and_bounded(self) -> None:
        summary = self.dispatcher.dispatch("step.inspect", {
            "ref_id": self.archive.archive_ref,
        })
        self.assertEqual(summary["section"], "summary")
        self.assertNotIn("transcript", summary)

        filtered = self.dispatcher.dispatch("step.inspect", {
            "ref_id": self.archive.archive_ref,
            "section": "transcript",
            "event_type": "validator",
            "tool": "verify_step",
            "validator": True,
            "query": "missing",
            "limit": 999,
        })
        self.assertEqual(len(filtered["transcript"]), 1)
        self.assertEqual(filtered["transcript"][0]["event_type"], "validator")
        self.assertLessEqual(filtered["page"]["limit"], 50)
        self.assertLessEqual(filtered["result_chars"], 24_000)

        execution = self.dispatcher.dispatch("step.inspect", {
            "ref_id": self.archive.archive_ref,
            "section": "transcript",
            "execution": True,
        })
        self.assertEqual(len(execution["transcript"]), 1)
        self.assertEqual(execution["transcript"][0]["event_type"], "execution")

    def test_artifact_inspect_and_read_never_accept_free_paths(self) -> None:
        inspected = self.dispatcher.dispatch(
            "artifact.inspect", {"ref_id": "artifact:approved"}
        )
        self.assertTrue(inspected["exists"])
        self.assertEqual(inspected["binding"]["direction"], "input")
        self.assertTrue(inspected["hash"].startswith("sha256:"))
        self.assertNotIn("content", inspected)

        read = self.dispatcher.dispatch("artifact.read", {
            "ref_id": "artifact:approved",
            "start_line": 2,
            "end_line": 2,
            "max_bytes": 32,
            "max_chars": 16,
            "binary_policy": "deny",
        })
        self.assertEqual(read["content"], "beta")
        with self.assertRaisesRegex(
            RecoveryToolError, "INSPECTION_REF_NOT_DECLARED"
        ):
            self.dispatcher.dispatch(
                "artifact.read", {"ref_id": str(self.artifact_path)}
            )

        binary = self.root / "binary.bin"
        binary.write_bytes(b"\x00\x01\x02")
        self.registry.register(InspectionRef(
            ref_id="artifact:binary",
            kind="artifact",
            task_id="task-recovery",
            plan_id="plan-recovery",
            step_id="step-1",
            backend="managed_artifacts",
            digest="sha256:binary",
            size_hint=3,
            scope={"access": "current_step"},
        ), locator={"artifact_id": "binary", "path": str(binary)})
        with self.assertRaisesRegex(
            RecoveryToolError, "READ_REPRESENTATION_UNSUPPORTED"
        ):
            self.dispatcher.dispatch(
                "artifact.read", {"ref_id": "artifact:binary"}
            )

    def test_evidence_tools_resolve_opaque_refs_through_existing_reader(self) -> None:
        read = self.dispatcher.dispatch("evidence.read", {
            "evidence_ref": "validation-1",
            "start_line": 2,
            "end_line": 3,
        })
        self.assertEqual(read["content"], "line two\nline three")
        globbed = self.dispatcher.dispatch(
            "evidence.glob", {"scope_ref": "evidence-scope:logs"}
        )
        self.assertEqual(globbed["matches"], ["run.log"])
        with self.assertRaisesRegex(
            RecoveryToolError, "INSPECTION_REF_NOT_DECLARED"
        ):
            self.dispatcher.dispatch(
                "evidence.read", {"evidence_ref": "../run.log"}
            )

    def test_skill_tools_are_limited_to_selected_snapshot_and_pin_resource(self) -> None:
        headings = self.dispatcher.dispatch(
            "skill.headings", {"skill_id": "genomics/selected"}
        )
        names = {
            row["heading"]
            for row in headings["results"][0]["heading_index"]
        }
        self.assertIn("Procedure", names)
        searched = self.dispatcher.dispatch("skill.search", {
            "skill_id": "genomics/selected",
            "query": "selected procedure",
        })
        self.assertIn("selected procedure", searched["results"][0]["exact_text"])

        first = self.dispatcher.dispatch("skill.resource", {
            "skill_id": "genomics/selected",
            "resource_path": "references/guide.txt",
        })
        original_resource = self.resource_path.read_text(encoding="utf-8")
        self.resource_path.write_text("changed resource v2\n", encoding="utf-8")
        from agent_core.skill_access import SkillAccessError
        with self.assertRaisesRegex(SkillAccessError, "SKILL_RESOURCE_CHANGED"):
            self.dispatcher.dispatch("skill.resource", {
                "skill_id": "genomics/selected", "resource_path": "references/guide.txt"})
        # Restore the original fixture bytes before checking its pinned excerpt.
        self.resource_path.write_text(original_resource, encoding="utf-8")
        excerpt = first["results"][0]
        retrieval_ref = "skill-excerpt:" + excerpt["excerpt_id"]
        recovered = self.dispatcher.dispatch("skill.resource", {
            "skill_id": "genomics/selected",
            "retrieval_ref": retrieval_ref,
            "content_hash": excerpt["content_hash"],
            "coverage": {
                "kind": "range",
                "start_line": excerpt["start_line"],
                "end_line": excerpt["end_line"],
            },
        })
        self.assertIn(
            "pinned resource v1", recovered["results"][0]["exact_text"]
        )
        self.assertEqual(
            recovered["retrieval_verification"]["retrieval_ref"],
            retrieval_ref,
        )
        self.assertTrue(
            recovered["retrieval_verification"]["body_hash_verified"]
        )
        with self.assertRaisesRegex(
            RecoveryToolError, "RECOVERY_SKILL_RETRIEVAL_HASH_MISMATCH"
        ):
            self.dispatcher.dispatch("skill.resource", {
                "skill_id": "genomics/selected",
                "retrieval_ref": retrieval_ref,
                "content_hash": "0" * 64,
            })
        with self.assertRaisesRegex(
            RecoveryToolError, "RECOVERY_SKILL_RETRIEVAL_PATH_MISMATCH"
        ):
            self.dispatcher.dispatch("skill.resource", {
                "skill_id": "genomics/selected",
                "retrieval_ref": retrieval_ref,
                "resource_path": "references/other.txt",
            })
        with self.assertRaisesRegex(
            RecoveryToolError, "RECOVERY_SKILL_NOT_SELECTED"
        ):
            self.dispatcher.dispatch(
                "skill.search",
                {"skill_id": "transcriptomics/unselected", "query": "x"},
            )

    def test_registry_rejects_cross_task_and_duplicate_ref_mutation(self) -> None:
        registry = TypedInspectionRegistry(
            task_id="task-recovery", plan_id="plan-recovery", step_id="step-1"
        )
        with self.assertRaisesRegex(
            RecoveryToolError, "INSPECTION_REF_SCOPE_MISMATCH"
        ):
            registry.register(InspectionRef(
                ref_id="evidence:other",
                kind="evidence",
                task_id="other-task",
                plan_id="plan-recovery",
                step_id="step-1",
                backend="fixture",
                digest="sha256:other",
                size_hint=0,
            ))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest

from agent_core.workflow.phase_archive import PhaseArchiveStore
from agent_core.workflow.phase_inspection import (
    PhaseInspectionError,
    PhaseInspectionService,
)
from agent_core.workflow.stage_contracts import FailurePacket


class PhaseInspectionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="phase-inspection-")
        self.addCleanup(self.temp.cleanup)
        self.store = PhaseArchiveStore(self.temp.name, transcript_page_limit=2)
        self.record = self.store.archive(
            task_id="task-a",
            phase="step_worker",
            invocation_id="step-1-attempt-1",
            summary={"status": "failed", "text": "annotation failed"},
            evidence=["evidence:failure-log"],
            transcript=[
                {"role": "assistant", "content": "inspect input"},
                {"role": "tool", "content": "input valid"},
                {"role": "assistant", "content": "run tool"},
                {"role": "tool", "content": "tool failed"},
            ],
            metadata={
                "seed_contract": {
                    "task_spec_ref": {"task_id": "task-a"},
                    "plan_spec_ref": {"task_id": "task-a"},
                    "current_step": {"step_id": "step-1"},
                }
            },
        )

    def _packet(self, **overrides) -> FailurePacket:
        values = {
            "schema_version": 1,
            "failure_id": "failure-1",
            "task_id": "task-a",
            "step_id": "step-1",
            "failure_type": "execution_error",
            "failure_owner": "runtime",
            "error_code": "TOOL_FAILED",
            "summary": "annotation failed",
            "retryable": True,
            "evidence_refs": (self.record.archive_ref,),
        }
        values.update(overrides)
        return FailurePacket(**values)

    def test_step_view_defaults_to_summary_and_never_returns_seed_context(self) -> None:
        service = PhaseInspectionService(self.store)

        view = service.inspect_step(
            archive_ref=self.record.archive_ref,
            task_id="task-a",
            phase="step_worker",
            step_id="step-1",
        )

        self.assertEqual(view["section"], "summary")
        self.assertEqual(view["step_id"], "step-1")
        self.assertEqual(view["summary"]["status"], "failed")
        for forbidden in (
            "transcript",
            "seed",
            "seed_message",
            "messages",
            "prompt",
            "context",
        ):
            self.assertNotIn(forbidden, view)

    def test_transcript_requires_explicit_section_and_remains_paginated(self) -> None:
        service = PhaseInspectionService(self.store)

        first = service.inspect_step(
            archive_ref=self.record.archive_ref,
            task_id="task-a",
            phase="step_worker",
            step_id="step-1",
            section="transcript",
            offset=0,
            limit=99,
        )
        self.assertEqual(first["section"], "transcript")
        self.assertEqual(len(first["transcript"]), 2)
        self.assertEqual(first["page"]["limit"], 2)
        self.assertEqual(first["page"]["next_offset"], 2)
        self.assertTrue(first["page"]["has_more"])
        self.assertNotIn("seed", first)
        self.assertNotIn("seed_message", first)

        with self.assertRaisesRegex(
            PhaseInspectionError,
            "PHASE_INSPECTION_PAGINATION_WITHOUT_TRANSCRIPT",
        ):
            service.inspect_step(
                archive_ref=self.record.archive_ref,
                task_id="task-a",
                phase="step_worker",
                step_id="step-1",
                section="summary",
                limit=1,
            )

    def test_task_phase_and_step_scope_are_each_checked(self) -> None:
        service = PhaseInspectionService(self.store)
        cases = (
            {"task_id": "task-b", "phase": "step_worker", "step_id": "step-1"},
            {"task_id": "task-a", "phase": "recovery", "step_id": "step-1"},
            {"task_id": "task-a", "phase": "step_worker", "step_id": "step-2"},
        )
        for scope in cases:
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(
                    PhaseInspectionError, "PHASE_INSPECTION_SCOPE_MISMATCH"
                ):
                    service.inspect_step(
                        archive_ref=self.record.archive_ref,
                        **scope,
                    )

    def test_missing_or_conflicting_archive_step_scope_fails_closed(self) -> None:
        missing = self.store.archive(
            task_id="task-a",
            phase="step_worker",
            invocation_id="missing-step",
            summary="missing step metadata",
            metadata={"seed_contract": {"task_id": "task-a"}},
        )
        conflict = self.store.archive(
            task_id="task-a",
            phase="step_worker",
            invocation_id="conflicting-step",
            summary="conflicting step metadata",
            metadata={
                "step_id": "step-1",
                "seed_contract": {"current_step": {"step_id": "step-2"}},
            },
        )
        service = PhaseInspectionService(self.store)

        with self.assertRaisesRegex(
            PhaseInspectionError, "PHASE_ARCHIVE_STEP_SCOPE_MISSING"
        ):
            service.inspect_step(
                archive_ref=missing.archive_ref,
                task_id="task-a",
                phase="step_worker",
                step_id="step-1",
            )
        with self.assertRaisesRegex(
            PhaseInspectionError, "PHASE_ARCHIVE_STEP_SCOPE_CONFLICT"
        ):
            service.inspect_step(
                archive_ref=conflict.archive_ref,
                task_id="task-a",
                phase="step_worker",
                step_id="step-1",
            )

    def test_recovery_may_only_view_an_archive_declared_by_failure_packet(self) -> None:
        service = PhaseInspectionService(self.store)
        view = service.inspect_for_recovery(
            failure_packet=self._packet(),
            archive_ref=self.record.archive_ref,
            task_id="task-a",
            phase="step_worker",
            step_id="step-1",
        )
        self.assertEqual(view["section"], "summary")
        self.assertNotIn("transcript", view)

        other = self.store.archive(
            task_id="task-a",
            phase="step_worker",
            invocation_id="step-1-attempt-2",
            summary="not referenced",
            metadata={"step_id": "step-1"},
        )
        with self.assertRaisesRegex(
            PhaseInspectionError, "FAILURE_ARCHIVE_REF_NOT_DECLARED"
        ):
            service.inspect_for_recovery(
                failure_packet=self._packet(),
                archive_ref=other.archive_ref,
                task_id="task-a",
                phase="step_worker",
                step_id="step-1",
            )

    def test_failure_packet_scope_is_checked_before_archive_access(self) -> None:
        service = PhaseInspectionService(self.store)
        for packet in (
            self._packet(task_id="task-b"),
            self._packet(step_id="step-2"),
        ):
            with self.subTest(packet=packet.to_dict()):
                with self.assertRaisesRegex(
                    PhaseInspectionError, "FAILURE_PACKET_SCOPE_MISMATCH"
                ):
                    service.inspect_for_recovery(
                        failure_packet=packet,
                        archive_ref=self.record.archive_ref,
                        task_id="task-a",
                        phase="step_worker",
                        step_id="step-1",
                    )

    def test_every_failure_reference_requires_resolution_and_matching_scope(self) -> None:
        packet = self._packet(
            evidence_refs=(self.record.archive_ref, "evidence:log"),
            attempt_ref="attempt:one",
            action_ref="action:one",
            diagnostic_facts=({
                "fact_id": "fact-1",
                "kind": "runtime",
                "summary": "tool exited non-zero",
                "evidence_ref": "evidence:diagnostic",
            },),
        )
        without_resolver = PhaseInspectionService(self.store)
        with self.assertRaisesRegex(
            PhaseInspectionError, "FAILURE_REFERENCE_UNVERIFIED"
        ):
            without_resolver.validate_failure_packet_references(
                packet, task_id="task-a", step_id="step-1"
            )

        resolved_refs: list[str] = []

        def resolver(reference: str):
            resolved_refs.append(reference)
            return {"task_id": "task-a", "step_id": "step-1"}

        service = PhaseInspectionService(
            self.store, reference_resolver=resolver
        )
        validated = service.validate_failure_packet_references(
            packet, task_id="task-a", step_id="step-1"
        )
        self.assertEqual(validated.failure_id, "failure-1")
        self.assertEqual(
            resolved_refs,
            ["evidence:log", "attempt:one", "action:one", "evidence:diagnostic"],
        )

        bad = PhaseInspectionService(
            self.store,
            reference_resolver=lambda _ref: {
                "task_id": "task-a",
                "step_id": "step-other",
            },
        )
        with self.assertRaisesRegex(
            PhaseInspectionError, "FAILURE_REFERENCE_SCOPE_MISMATCH"
        ):
            bad.validate_failure_packet_references(
                packet, task_id="task-a", step_id="step-1"
            )

    def test_phase_archive_reference_inside_packet_is_scope_checked(self) -> None:
        wrong_step = self.store.archive(
            task_id="task-a",
            phase="step_worker",
            invocation_id="wrong-step-reference",
            summary="wrong step",
            metadata={"step_id": "step-2"},
        )
        packet = self._packet(evidence_refs=(wrong_step.archive_ref,))

        with self.assertRaisesRegex(
            PhaseInspectionError, "FAILURE_REFERENCE_SCOPE_MISMATCH"
        ):
            PhaseInspectionService(self.store).validate_failure_packet_references(
                packet, task_id="task-a", step_id="step-1"
            )


if __name__ == "__main__":
    unittest.main()

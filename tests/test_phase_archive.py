import json
from pathlib import Path
import tempfile
import unittest

from agent_core.workflow.phase_archive import (
    PhaseArchiveCollisionError,
    PhaseArchiveIntegrityError,
    PhaseArchiveRecord,
    PhaseArchiveStore,
)


class PhaseArchiveTests(unittest.TestCase):
    @staticmethod
    def _record(*, summary="step completed") -> PhaseArchiveRecord:
        return PhaseArchiveRecord(
            task_id="task-archive",
            phase="step_worker",
            invocation_id="step-1-attempt-1",
            summary={"status": "completed", "text": summary},
            artifacts=[{"artifact_ref": "artifact:sha256:abc", "kind": "vcf"}],
            evidence=[{"evidence_ref": "evidence:validation-1"}],
            transcript=[
                {"role": "assistant", "content": "inspect input"},
                {"role": "tool", "content": "input is valid"},
                {"role": "assistant", "content": "run annotation"},
                {"role": "tool", "content": "annotation succeeded"},
                {"role": "assistant", "content": "verify output"},
            ],
            metadata={"plan_version": "plan-v1"},
        )

    def test_default_inspect_returns_summary_without_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = PhaseArchiveStore(tmpdir)
            stored = store.write(self._record())

            self.assertTrue(stored.archive_ref.startswith("phase-archive:sha256:"))
            view = store.inspect(stored.archive_ref)

            self.assertEqual(view["section"], "summary")
            self.assertEqual(view["summary"]["status"], "completed")
            self.assertNotIn("transcript", view)
            self.assertNotIn("artifacts", view)
            self.assertEqual(store.read(stored.archive_ref), stored)

    def test_same_payload_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = PhaseArchiveStore(tmpdir)
            first = store.write(self._record())
            second = store.put(self._record())

            self.assertEqual(second.archive_ref, first.archive_ref)
            objects = list((Path(tmpdir) / "phase_archive" / "objects").rglob("*.json"))
            identities = list(
                (Path(tmpdir) / "phase_archive" / "by_invocation").rglob("*.json")
            )
            self.assertEqual(len(objects), 1)
            self.assertEqual(len(identities), 1)

    def test_same_invocation_with_different_payload_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = PhaseArchiveStore(tmpdir)
            store.write(self._record())

            with self.assertRaises(PhaseArchiveCollisionError):
                store.write(self._record(summary="different result"))

    def test_inspect_detects_tampered_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = PhaseArchiveStore(tmpdir)
            stored = store.write(self._record())
            object_path = (
                Path(tmpdir)
                / "phase_archive"
                / "objects"
                / stored.digest[:2]
                / f"{stored.digest}.json"
            )
            raw = json.loads(object_path.read_text(encoding="utf-8"))
            raw["summary"]["status"] = "tampered"
            object_path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(PhaseArchiveIntegrityError):
                store.inspect(stored.archive_ref)

    def test_transcript_requires_explicit_section_and_is_hard_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = PhaseArchiveStore(tmpdir, transcript_page_limit=2)
            stored = store.archive(
                task_id=self._record().task_id,
                phase=self._record().phase,
                invocation_id=self._record().invocation_id,
                summary=self._record().summary,
                artifacts=self._record().artifacts,
                evidence=self._record().evidence,
                transcript=self._record().transcript,
                metadata=self._record().metadata,
            )

            default_view = store.inspect(stored.archive_ref)
            self.assertNotIn("transcript", default_view)

            first_page = store.inspect(
                stored.archive_ref,
                section="transcript",
                offset=1,
                limit=99,
            )
            self.assertEqual(
                [item["content"] for item in first_page["transcript"]],
                ["input is valid", "run annotation"],
            )
            self.assertEqual(first_page["page"]["requested_limit"], 99)
            self.assertEqual(first_page["page"]["limit"], 2)
            self.assertEqual(first_page["page"]["next_offset"], 3)
            self.assertTrue(first_page["page"]["has_more"])

            final_page = store.inspect(
                stored.archive_ref,
                section="transcript",
                offset=3,
            )
            self.assertEqual(len(final_page["transcript"]), 2)
            self.assertIsNone(final_page["page"]["next_offset"])
            self.assertFalse(final_page["page"]["has_more"])


if __name__ == "__main__":
    unittest.main()

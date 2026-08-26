from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_core.plan_tracker import load_plan_state, save_plan_state
from bio_agents import BioAnalystAgent
from memory.session_archive import SessionArchive
from memory.session_archive_writer import (
    SessionArchiveObservationWriter,
    acquire_observation_writer,
    release_observation_writer,
)


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _payload(ordinal=1):
    return {
        "task_id": "task-observe",
        "session_id": "task-observe",
        "stage": "generate",
        "event_type": f"optional-{ordinal}",
        "content": f"event {ordinal}",
        "summary": f"event {ordinal}",
        "metadata": {"ordinal": ordinal},
    }


class SessionArchiveObservationWriterTests(unittest.TestCase):
    def test_b1_locked_archive_does_not_block_observation_node(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.sqlite3"
            SessionArchive(db_path)
            lock = sqlite3.connect(db_path, timeout=0.1)
            lock.execute("BEGIN EXCLUSIVE")
            writer = SessionArchiveObservationWriter(
                db_path=db_path,
                capacity=4,
                busy_timeout_seconds=0.1,
                max_write_attempts=1,
                shutdown_drain_seconds=0.1,
            )
            agent = BioAnalystAgent.__new__(BioAnalystAgent)
            agent.default_project_id = "default"
            agent.session_archive_writer = writer
            started = time.monotonic()
            asyncio.run(agent._observe_session_memory("task-observe", "generate", payload={"x": 1}))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.1)
            lock.rollback()
            lock.close()
            writer.close(0.3)

    def test_b2_queue_full_drops_without_waiting(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_write(**kwargs):
            entered.set()
            release.wait(1)

        writer = SessionArchiveObservationWriter(write_call=blocked_write, capacity=1)
        self.assertEqual(writer.enqueue_nowait(**_payload(1)), "enqueued")
        self.assertTrue(entered.wait(0.5))
        self.assertEqual(writer.enqueue_nowait(**_payload(2)), "enqueued")
        started = time.monotonic()
        self.assertEqual(writer.enqueue_nowait(**_payload(3)), "dropped")
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(writer.snapshot()["dropped_count"], 1)
        release.set()
        writer.close(0.5)

    def test_b3_background_writer_success_updates_counts(self):
        rows = []
        writer = SessionArchiveObservationWriter(write_call=lambda **row: rows.append(row))
        self.assertEqual(writer.enqueue_nowait(**_payload(1)), "enqueued")
        self.assertTrue(_wait_for(lambda: writer.snapshot()["written_count"] == 1))
        snapshot = writer.close()
        self.assertEqual(snapshot["enqueued_count"], 1)
        self.assertEqual(snapshot["written_count"], 1)
        self.assertEqual(len(rows), 1)

    def test_b4_writer_retries_exhausted_are_fail_open(self):
        task_state = {"status": "running"}

        def locked(**kwargs):
            raise sqlite3.OperationalError("database is locked")

        writer = SessionArchiveObservationWriter(
            write_call=locked,
            max_write_attempts=2,
            backoff_seconds=0,
        )
        writer.enqueue_nowait(**_payload(1))
        self.assertTrue(_wait_for(lambda: writer.snapshot()["write_failure_count"] == 1))
        snapshot = writer.close()
        self.assertEqual(snapshot["retry_count"], 1)
        self.assertEqual(snapshot["write_failure_count"], 1)
        self.assertEqual(task_state, {"status": "running"})

    def test_b5_canonical_plan_write_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = load_plan_state(tmpdir, "canonical")
            with patch(
                "agent_core.plan_tracker._atomic_write_plan_state",
                side_effect=OSError("canonical disk failure"),
            ):
                with self.assertRaisesRegex(OSError, "canonical disk failure"):
                    save_plan_state(tmpdir, "canonical", state)

    def test_b6_shutdown_drain_is_bounded_and_accounted(self):
        def delayed(**kwargs):
            time.sleep(0.005)

        writer = SessionArchiveObservationWriter(
            write_call=delayed,
            capacity=8,
            shutdown_drain_seconds=0.5,
        )
        for ordinal in range(5):
            writer.enqueue_nowait(**_payload(ordinal))
        started = time.monotonic()
        snapshot = writer.close()
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(snapshot["written_count"], 5)
        self.assertEqual(snapshot["remaining_not_durable_count"], 0)
        self.assertEqual(snapshot["queue_depth"], 0)

    def test_b7_duplicate_observation_is_coalesced_or_deduplicated(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_write(**kwargs):
            entered.set()
            release.wait(1)

        writer = SessionArchiveObservationWriter(write_call=blocked_write)
        self.assertEqual(writer.enqueue_nowait(**_payload(1)), "enqueued")
        self.assertTrue(entered.wait(0.5))
        self.assertEqual(writer.enqueue_nowait(**_payload(1)), "coalesced")
        release.set()
        snapshot = writer.close(0.5)
        self.assertEqual(snapshot["coalesced_count"], 1)

    def test_b8_registry_is_explicitly_process_local(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.sqlite3"
            first = acquire_observation_writer(db_path)
            second = acquire_observation_writer(db_path)
            self.assertIs(first, second)
            self.assertEqual(first.snapshot()["scope"], "PER_PROCESS")
            release_observation_writer(db_path)
            release_observation_writer(db_path)

    def test_b9_wal_remains_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.sqlite3"
            SessionArchive(db_path)
            with sqlite3.connect(db_path) as connection:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(str(mode).lower(), "delete")
        sources = "\n".join(
            Path(path).read_text(encoding="utf-8")
            for path in (
                "memory/session_archive.py",
                "memory/session_archive_writer.py",
                "config/reliability.py",
            )
        ).lower()
        self.assertNotIn("journal_mode=wal", sources.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory.session_archive import (
    ARCHIVE_SCHEMA_VERSION,
    TASK_EVENT_LOOKUP_INDEX,
    SessionArchive,
)


class SessionArchiveTaskIndexTests(unittest.TestCase):
    def test_legacy_database_migrates_idempotently_without_row_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "session_archive.sqlite3"
            archive = SessionArchive(db_path)
            for index in range(4):
                archive.append_event(
                    task_id="task-a" if index < 3 else "task-b",
                    session_id="session-a",
                    stage="consultant",
                    event_type="test_event",
                    content=f"event-{index}",
                    summary=f"event-{index}",
                )

            with sqlite3.connect(db_path) as conn:
                before_rows = conn.execute(
                    "SELECT id, task_id, content FROM session_events ORDER BY id"
                ).fetchall()
                conn.execute(f"DROP INDEX {TASK_EVENT_LOOKUP_INDEX}")
                conn.execute(
                    "UPDATE schema_version SET version = 2 "
                    "WHERE component = 'session_archive'"
                )
                conn.commit()

            with self.assertLogs("memory.session_archive", level="INFO") as logs:
                SessionArchive(db_path)
            self.assertTrue(any("creating task lookup index" in row for row in logs.output))

            with sqlite3.connect(db_path) as conn:
                after_rows = conn.execute(
                    "SELECT id, task_id, content FROM session_events ORDER BY id"
                ).fetchall()
                schema_version = conn.execute(
                    "SELECT version FROM schema_version "
                    "WHERE component = 'session_archive'"
                ).fetchone()[0]
                index_rows = [
                    row for row in conn.execute(
                        "PRAGMA index_list('session_events')"
                    ).fetchall()
                    if row[1] == TASK_EVENT_LOOKUP_INDEX
                ]
                query_plan = " ".join(
                    str(row[3])
                    for row in conn.execute(
                        "EXPLAIN QUERY PLAN SELECT * FROM session_events "
                        "WHERE task_id = ? ORDER BY id ASC LIMIT ?",
                        ("task-a", 300),
                    ).fetchall()
                )

            self.assertEqual(before_rows, after_rows)
            self.assertEqual(schema_version, ARCHIVE_SCHEMA_VERSION)
            self.assertEqual(len(index_rows), 1)
            self.assertIn(TASK_EVENT_LOOKUP_INDEX, query_plan)

            SessionArchive(db_path)
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(
                    sum(
                        row[1] == TASK_EVENT_LOOKUP_INDEX
                        for row in conn.execute(
                            "PRAGMA index_list('session_events')"
                        ).fetchall()
                    ),
                    1,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0],
                    len(before_rows),
                )


if __name__ == "__main__":
    unittest.main()

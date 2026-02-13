"""Tests for autopilot orphan detection and cleanup (issue #57)."""

import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

import importlib
import sys

root = Path(__file__).resolve().parents[1]
src = root / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

from orxaq_autonomy.autopilot_cleanup import (
    DEFAULT_ORPHAN_THRESHOLD_SEC,
    cleanup_on_startup,
    cleanup_orphans,
    detect_orphans,
)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _past_iso(seconds_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _create_test_db(db_path: Path) -> None:
    """Create a minimal autopilot SQLite database for testing."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 50,
            status TEXT NOT NULL DEFAULT 'pending',
            ab_variant TEXT DEFAULT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT DEFAULT NULL,
            completed_at TEXT DEFAULT NULL,
            result TEXT DEFAULT NULL,
            duration_sec REAL DEFAULT NULL,
            executor TEXT DEFAULT 'local'
        );
    """)
    conn.close()


def _insert_prompt(db_path: Path, content: str, status: str, started_at: str | None = None) -> int:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO prompts (content, status, created_at, started_at) VALUES (?, ?, ?, ?)",
        (content, status, _now_iso(), started_at),
    )
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return row_id


class TestDetectOrphans(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "autopilot.db"
        _create_test_db(self.db_path)

    def test_no_orphans_when_empty(self):
        result = detect_orphans(db_path=self.db_path, threshold_sec=1800)
        self.assertEqual(result, [])

    def test_no_orphans_when_all_pending(self):
        _insert_prompt(self.db_path, "task 1", "pending")
        _insert_prompt(self.db_path, "task 2", "completed")
        result = detect_orphans(db_path=self.db_path, threshold_sec=1800)
        self.assertEqual(result, [])

    def test_running_within_threshold_not_orphan(self):
        # Task started 5 minutes ago, threshold is 30 minutes
        _insert_prompt(self.db_path, "recent task", "running", started_at=_past_iso(300))
        result = detect_orphans(db_path=self.db_path, threshold_sec=1800)
        self.assertEqual(result, [])

    def test_running_beyond_threshold_is_orphan(self):
        # Task started 2 hours ago, threshold is 30 minutes
        _insert_prompt(self.db_path, "stuck task", "running", started_at=_past_iso(7200))
        result = detect_orphans(db_path=self.db_path, threshold_sec=1800)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "stuck task")
        self.assertGreaterEqual(result[0]["age_sec"], 7200)

    def test_running_with_no_started_at_is_orphan(self):
        _insert_prompt(self.db_path, "no timestamp task", "running", started_at=None)
        result = detect_orphans(db_path=self.db_path, threshold_sec=1800)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "no timestamp task")

    def test_multiple_orphans(self):
        _insert_prompt(self.db_path, "old task 1", "running", started_at=_past_iso(3600))
        _insert_prompt(self.db_path, "old task 2", "running", started_at=_past_iso(5400))
        _insert_prompt(self.db_path, "recent task", "running", started_at=_past_iso(60))
        result = detect_orphans(db_path=self.db_path, threshold_sec=1800)
        self.assertEqual(len(result), 2)

    def test_nonexistent_db_returns_empty(self):
        result = detect_orphans(
            db_path=Path(self.tmpdir) / "nonexistent.db",
            threshold_sec=1800,
        )
        self.assertEqual(result, [])


class TestCleanupOrphans(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "autopilot.db"
        _create_test_db(self.db_path)

    def test_cleanup_marks_orphans_as_failed(self):
        prompt_id = _insert_prompt(
            self.db_path, "stuck task", "running", started_at=_past_iso(7200)
        )
        cleaned = cleanup_orphans(db_path=self.db_path, threshold_sec=1800)
        self.assertEqual(len(cleaned), 1)

        # Verify in database
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT status, result FROM prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "failed")
        result_data = json.loads(row[1])
        self.assertEqual(result_data["reason"], "orphaned")

    def test_cleanup_does_not_touch_healthy_tasks(self):
        pending_id = _insert_prompt(self.db_path, "pending task", "pending")
        done_id = _insert_prompt(self.db_path, "done task", "completed")
        recent_id = _insert_prompt(
            self.db_path, "recent running", "running", started_at=_past_iso(60)
        )

        cleaned = cleanup_orphans(db_path=self.db_path, threshold_sec=1800)
        self.assertEqual(len(cleaned), 0)

        conn = sqlite3.connect(str(self.db_path))
        for row_id, expected_status in [
            (pending_id, "pending"),
            (done_id, "completed"),
            (recent_id, "running"),
        ]:
            row = conn.execute(
                "SELECT status FROM prompts WHERE id = ?", (row_id,)
            ).fetchone()
            self.assertEqual(row[0], expected_status)
        conn.close()

    def test_cleanup_custom_reason(self):
        prompt_id = _insert_prompt(
            self.db_path, "stuck", "running", started_at=_past_iso(7200)
        )
        cleanup_orphans(
            db_path=self.db_path, threshold_sec=1800, reason="crash_recovery"
        )

        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT result FROM prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
        conn.close()
        result_data = json.loads(row[0])
        self.assertEqual(result_data["reason"], "crash_recovery")


class TestCleanupOnStartup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "autopilot.db"
        _create_test_db(self.db_path)

    def test_cleanup_on_startup_returns_cleaned(self):
        _insert_prompt(self.db_path, "orphan", "running", started_at=_past_iso(7200))
        result = cleanup_on_startup(
            db_path=self.db_path, threshold_sec=1800, quiet=True
        )
        self.assertEqual(len(result), 1)

    def test_cleanup_on_startup_empty_db(self):
        result = cleanup_on_startup(
            db_path=self.db_path, threshold_sec=1800, quiet=True
        )
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()

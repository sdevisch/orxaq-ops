"""Autopilot orphan detection and cleanup.

The autopilot system (~/.claude/autopilot/) persists task state in a SQLite
database. If the autopilot process crashes while a task is in 'running' state,
the task is permanently stuck because nothing resets it to 'pending' or 'failed'.

This module provides:
- ``detect_orphans``: find tasks stuck in 'running' longer than a threshold.
- ``cleanup_orphans``: mark orphaned tasks as 'failed' with a reason.
- ``cleanup_on_startup``: convenience function to run cleanup at process start.

Fixes: https://github.com/Orxaq/orxaq-ops/issues/57
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path.home() / ".claude" / "autopilot" / "autopilot.db"
DEFAULT_ORPHAN_THRESHOLD_SEC = 1800  # 30 minutes


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso_epoch(ts: str) -> float | None:
    """Parse ISO timestamp to epoch seconds. Returns None on failure."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return parsed.timestamp()
    except (ValueError, TypeError):
        return None


def detect_orphans(
    db_path: Path = DEFAULT_DB_PATH,
    threshold_sec: float = DEFAULT_ORPHAN_THRESHOLD_SEC,
) -> list[dict[str, Any]]:
    """Find autopilot tasks stuck in 'running' state beyond the threshold.

    Returns a list of dicts with keys: id, content, started_at, age_sec.
    """
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, content, started_at FROM prompts WHERE status = 'running'"
        ).fetchall()
    except sqlite3.OperationalError:
        # Table may not exist yet
        conn.close()
        return []

    now = time.time()
    orphans: list[dict[str, Any]] = []
    for row in rows:
        started_epoch = _parse_iso_epoch(row["started_at"] or "")
        if started_epoch is None:
            # No valid timestamp — treat as orphan immediately
            age_sec = threshold_sec + 1
        else:
            age_sec = now - started_epoch

        if age_sec >= threshold_sec:
            orphans.append(
                {
                    "id": row["id"],
                    "content": (row["content"] or "")[:200],
                    "started_at": row["started_at"] or "",
                    "age_sec": round(age_sec, 1),
                }
            )

    conn.close()
    return orphans


def cleanup_orphans(
    db_path: Path = DEFAULT_DB_PATH,
    threshold_sec: float = DEFAULT_ORPHAN_THRESHOLD_SEC,
    reason: str = "orphaned",
) -> list[dict[str, Any]]:
    """Detect and mark orphaned tasks as 'failed'.

    Returns the list of cleaned-up orphans (same format as detect_orphans).
    """
    orphans = detect_orphans(db_path=db_path, threshold_sec=threshold_sec)
    if not orphans:
        return []

    conn = sqlite3.connect(str(db_path))
    now_str = _now_iso()
    for orphan in orphans:
        result_text = json.dumps(
            {"reason": reason, "cleaned_at": now_str, "age_sec": orphan["age_sec"]}
        )
        conn.execute(
            "UPDATE prompts SET status = 'failed', completed_at = ?, result = ? WHERE id = ?",
            (now_str, result_text, orphan["id"]),
        )
    conn.commit()
    conn.close()
    return orphans


def cleanup_on_startup(
    db_path: Path = DEFAULT_DB_PATH,
    threshold_sec: float = DEFAULT_ORPHAN_THRESHOLD_SEC,
    quiet: bool = False,
) -> list[dict[str, Any]]:
    """Run orphan cleanup — intended to be called at process startup.

    Prints a summary unless quiet=True. Returns cleaned orphans.
    """
    cleaned = cleanup_orphans(db_path=db_path, threshold_sec=threshold_sec)
    if cleaned and not quiet:
        print(f"[autopilot_cleanup] Cleaned {len(cleaned)} orphaned task(s):")
        for item in cleaned:
            print(
                f"  - id={item['id']} started_at={item['started_at']} "
                f"age={item['age_sec']}s content={item['content'][:80]}..."
            )
    return cleaned

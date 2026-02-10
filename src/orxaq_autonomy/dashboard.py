"""Dashboard helpers for distributed todo metrics."""

from __future__ import annotations

from typing import Any


def _to_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if not isinstance(value, str):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def normalize_todo_coverage_metrics(summary: dict[str, Any] | None) -> dict[str, int]:
    """Return internally consistent todo coverage totals.

    Rules:
    - `covered`/`uncovered` are clamped to non-negative ints.
    - `total` is exactly `covered + uncovered`.
    - provided total values are ignored when inconsistent.
    """

    data = summary if isinstance(summary, dict) else {}
    covered = _to_non_negative_int(data.get("live_covered"))
    uncovered = _to_non_negative_int(data.get("live_uncovered"))
    total = covered + uncovered
    return {
        "live_covered": covered,
        "live_uncovered": uncovered,
        "live_coverage_total": total,
    }

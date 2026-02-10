"""Dashboard helpers for distributed todo metrics."""

from __future__ import annotations

from typing import Any


def _to_non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def normalize_todo_coverage_metrics(summary: dict[str, Any] | None) -> dict[str, int]:
    """Return internally consistent todo coverage totals.

    Rules:
    - `covered`/`uncovered` are clamped to non-negative ints.
    - `total` is at least `covered + uncovered`.
    - if provided total is lower than covered+uncovered, the derived sum wins.
    """

    data = summary if isinstance(summary, dict) else {}
    covered = _to_non_negative_int(data.get("live_covered"))
    uncovered = _to_non_negative_int(data.get("live_uncovered"))
    derived_total = covered + uncovered
    provided_total = _to_non_negative_int(data.get("live_coverage_total"))
    total = max(derived_total, provided_total)
    return {
        "live_covered": covered,
        "live_uncovered": uncovered,
        "live_coverage_total": total,
    }

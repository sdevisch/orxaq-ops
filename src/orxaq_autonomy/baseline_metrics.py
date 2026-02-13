"""Deterministic baseline metrics pipeline for KPI baselining.

Captures validation runtimes, retry counts, failure signatures, and lane queue
timing as machine-readable JSON artifacts under the metrics directory.

Produces repeatable, regression-safe output suitable for T1/T2/T3/T6 KPI
instrumentation.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ValidationMetric:
    """A single validation command execution metric."""

    command: str
    duration_ms: float
    exit_code: int
    passed: bool
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetryMetric:
    """Retry event metric for a single task."""

    task_id: str
    attempt: int
    retryable: bool
    backoff_sec: float
    error_signature: str
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LaneTimingMetric:
    """Lane queue wait time metric."""

    lane: str
    task_id: str
    queue_wait_ms: float
    execution_ms: float
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FailureSignature:
    """Normalized failure signature extracted from error text."""

    category: str  # timeout, rate_limit, network, validation, unknown
    pattern: str
    count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Failure classification patterns
_FAILURE_PATTERNS: list[tuple[str, str]] = [
    ("timeout", r"(?i)(timeout|timed.out|deadline.exceeded)"),
    ("rate_limit", r"(?i)(rate.limit|429|too.many.requests)"),
    ("network", r"(?i)(network|connection.reset|connection.aborted|unavailable)"),
    ("validation", r"(?i)(assertion|test.failed|lint.error|validation.failed)"),
    ("git_lock", r"(?i)(index\.lock|another.git.process)"),
    ("auth", r"(?i)(auth|unauthorized|forbidden|403|401)"),
]


def classify_failure(error_text: str) -> str:
    """Classify an error text into a failure category."""
    for category, pattern in _FAILURE_PATTERNS:
        if re.search(pattern, error_text):
            return category
    return "unknown"


def extract_failure_signatures(errors: list[str]) -> list[FailureSignature]:
    """Extract and aggregate failure signatures from a list of error texts."""
    counts: dict[str, int] = {}
    patterns: dict[str, str] = {}
    for error in errors:
        category = classify_failure(error)
        counts[category] = counts.get(category, 0) + 1
        if category not in patterns:
            # Store first occurrence as representative pattern
            patterns[category] = error[:200].strip()
    return [
        FailureSignature(category=cat, pattern=patterns.get(cat, ""), count=cnt)
        for cat, cnt in sorted(counts.items())
    ]


@dataclass
class BaselineMetricsCollector:
    """Collects baseline metrics during an autonomy run.

    Metrics are accumulated in memory and flushed to disk as JSON artifacts
    at configurable intervals or on explicit flush.
    """

    validation_metrics: list[ValidationMetric] = field(default_factory=list)
    retry_metrics: list[RetryMetric] = field(default_factory=list)
    lane_timing_metrics: list[LaneTimingMetric] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def record_validation(
        self,
        command: str,
        duration_ms: float,
        exit_code: int,
        passed: bool,
    ) -> None:
        self.validation_metrics.append(
            ValidationMetric(
                command=command,
                duration_ms=round(duration_ms, 3),
                exit_code=exit_code,
                passed=passed,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

    def record_retry(
        self,
        task_id: str,
        attempt: int,
        retryable: bool,
        backoff_sec: float,
        error_text: str,
    ) -> None:
        self.retry_metrics.append(
            RetryMetric(
                task_id=task_id,
                attempt=attempt,
                retryable=retryable,
                backoff_sec=backoff_sec,
                error_signature=classify_failure(error_text),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.errors.append(error_text)

    def record_lane_timing(
        self,
        lane: str,
        task_id: str,
        queue_wait_ms: float,
        execution_ms: float,
    ) -> None:
        self.lane_timing_metrics.append(
            LaneTimingMetric(
                lane=lane,
                task_id=task_id,
                queue_wait_ms=round(queue_wait_ms, 3),
                execution_ms=round(execution_ms, 3),
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )

    def aggregate(self) -> dict[str, Any]:
        """Produce aggregated baseline metrics report."""
        # Validation stats
        val_durations = [m.duration_ms for m in self.validation_metrics]
        val_pass_count = sum(1 for m in self.validation_metrics if m.passed)
        val_fail_count = len(self.validation_metrics) - val_pass_count

        # Retry stats
        retry_total = len(self.retry_metrics)
        retry_retryable = sum(1 for r in self.retry_metrics if r.retryable)
        retry_non_retryable = retry_total - retry_retryable
        backoffs = [r.backoff_sec for r in self.retry_metrics]

        # Lane timing stats per lane
        lane_stats: dict[str, dict[str, Any]] = {}
        for m in self.lane_timing_metrics:
            if m.lane not in lane_stats:
                lane_stats[m.lane] = {"queue_waits": [], "executions": [], "count": 0}
            lane_stats[m.lane]["queue_waits"].append(m.queue_wait_ms)
            lane_stats[m.lane]["executions"].append(m.execution_ms)
            lane_stats[m.lane]["count"] += 1

        lane_summaries = {}
        for lane, data in sorted(lane_stats.items()):
            lane_summaries[lane] = {
                "count": data["count"],
                "queue_wait_p50_ms": _percentile(data["queue_waits"], 50),
                "queue_wait_p95_ms": _percentile(data["queue_waits"], 95),
                "execution_p50_ms": _percentile(data["executions"], 50),
                "execution_p95_ms": _percentile(data["executions"], 95),
            }

        # Failure signatures
        signatures = extract_failure_signatures(self.errors)

        return {
            "schema_version": "baseline-metrics.v1",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "validation": {
                "total": len(self.validation_metrics),
                "passed": val_pass_count,
                "failed": val_fail_count,
                "duration_p50_ms": _percentile(val_durations, 50),
                "duration_p95_ms": _percentile(val_durations, 95),
                "duration_mean_ms": round(statistics.mean(val_durations), 3) if val_durations else 0.0,
            },
            "retries": {
                "total": retry_total,
                "retryable": retry_retryable,
                "non_retryable": retry_non_retryable,
                "backoff_mean_sec": round(statistics.mean(backoffs), 3) if backoffs else 0.0,
                "backoff_max_sec": round(max(backoffs), 3) if backoffs else 0.0,
            },
            "lane_timing": lane_summaries,
            "failure_signatures": [s.to_dict() for s in signatures],
        }

    def flush(self, output_dir: Path) -> Path:
        """Write current metrics to disk and return the output path."""
        output_dir.mkdir(parents=True, exist_ok=True)
        report = self.aggregate()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = output_dir / f"baseline_metrics_{ts}.json"
        output_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return output_path


def _percentile(data: list[float], pct: int) -> float:
    """Compute the nth percentile of a list."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * pct / 100.0
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return round(sorted_data[f], 3)
    d0 = sorted_data[f] * (c - k)
    d1 = sorted_data[c] * (k - f)
    return round(d0 + d1, 3)


def parse_state_for_retry_metrics(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract retry-related metrics from a runner state dict."""
    results: list[dict[str, Any]] = []
    for task_id, entry in state.items():
        if not isinstance(entry, dict):
            continue
        attempts = int(entry.get("attempts", 0) or 0)
        retryable_failures = int(entry.get("retryable_failures", 0) or 0)
        status = str(entry.get("status", ""))
        last_error = str(entry.get("last_error", ""))
        if attempts > 0 or retryable_failures > 0:
            results.append({
                "task_id": str(task_id),
                "status": status,
                "attempts": attempts,
                "retryable_failures": retryable_failures,
                "failure_category": classify_failure(last_error) if last_error else "",
            })
    return results


def parse_budget_for_metrics(budget: dict[str, Any]) -> dict[str, Any]:
    """Extract KPI-relevant fields from a budget report."""
    totals = budget.get("totals", {})
    limits = budget.get("limits", {})
    return {
        "elapsed_sec": int(budget.get("elapsed_sec", 0)),
        "tokens_used": int(totals.get("tokens", 0)),
        "cost_usd": float(totals.get("cost_usd", 0.0)),
        "retry_events": int(totals.get("retry_events", 0)),
        "max_runtime_sec": int(limits.get("max_runtime_sec", 0)),
        "max_tokens": int(limits.get("max_total_tokens", 0)),
        "violations": budget.get("violations", []),
    }

"""Collaboration health monitor for continuous degradation detection.

Detects collaboration degradation signals such as lane stoppages, idle
behavior, and validation bottlenecks.  Produces root-cause diagnoses and
delegates remediation tasks to lower-tier lanes.

Zero external dependencies -- uses only Python stdlib.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Signal classification
# ---------------------------------------------------------------------------

class DegradationSignal(str, Enum):
    """Well-known degradation signal types."""
    LANE_STOPPAGE = "lane_stoppage"
    IDLE_BEHAVIOR = "idle_behavior"
    VALIDATION_BOTTLENECK = "validation_bottleneck"
    HEARTBEAT_STALE = "heartbeat_stale"
    BUDGET_EXCEEDED = "budget_exceeded"
    TASK_BLOCKED = "task_blocked"
    HIGH_FAILURE_RATE = "high_failure_rate"


class HealthGrade(str, Enum):
    """Overall collaboration health grade."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Diagnosis:
    """A root-cause diagnosis for a detected degradation signal."""
    signal: str
    severity: str  # "low", "medium", "high", "critical"
    root_cause: str
    evidence: dict[str, Any] = field(default_factory=dict)
    suggested_remediation: str = ""
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RemediationTask:
    """A remediation task to be delegated to a lower-tier lane."""
    task_id: str
    target_lane: str  # e.g. "L0", "L1"
    description: str
    diagnosis_signal: str
    priority: int = 1  # 0 = highest
    status: str = "pending"
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HealthStatus:
    """Current collaboration health status."""
    grade: str
    score: int  # 0-100
    diagnoses: list[Diagnosis] = field(default_factory=list)
    remediations: list[RemediationTask] = field(default_factory=list)
    timestamp: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "score": self.score,
            "diagnoses": [d.to_dict() for d in self.diagnoses],
            "remediations": [r.to_dict() for r in self.remediations],
            "timestamp": self.timestamp,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def detect_lane_stoppages(state: dict[str, Any]) -> list[Diagnosis]:
    """Detect lanes with blocked tasks suggesting a stoppage."""
    diagnoses: list[Diagnosis] = []
    blocked_tasks: list[str] = []
    for task_id, task_data in state.items():
        if not isinstance(task_data, dict):
            continue
        status = str(task_data.get("status", "")).strip().lower()
        if status == "blocked":
            blocked_tasks.append(str(task_id))
    if blocked_tasks:
        severity = "high" if len(blocked_tasks) >= 3 else "medium"
        diagnoses.append(Diagnosis(
            signal=DegradationSignal.LANE_STOPPAGE,
            severity=severity,
            root_cause=f"{len(blocked_tasks)} task(s) blocked: {', '.join(blocked_tasks[:5])}",
            evidence={"blocked_tasks": blocked_tasks},
            suggested_remediation="Inspect blocked tasks for root cause; unblock or reassign.",
        ))
    return diagnoses


def detect_idle_behavior(
    state: dict[str, Any],
    *,
    idle_threshold_sec: int = 1800,
) -> list[Diagnosis]:
    """Detect tasks that have not been updated within the idle threshold."""
    diagnoses: list[Diagnosis] = []
    idle_tasks: list[str] = []
    now = datetime.now(timezone.utc)
    for task_id, task_data in state.items():
        if not isinstance(task_data, dict):
            continue
        status = str(task_data.get("status", "")).strip().lower()
        if status in ("done", "blocked"):
            continue
        last_update = str(task_data.get("last_update", "")).strip()
        if not last_update:
            idle_tasks.append(str(task_id))
            continue
        try:
            parsed = datetime.fromisoformat(last_update)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age = (now - parsed).total_seconds()
            if age > idle_threshold_sec:
                idle_tasks.append(str(task_id))
        except (ValueError, TypeError):
            idle_tasks.append(str(task_id))
    if idle_tasks:
        diagnoses.append(Diagnosis(
            signal=DegradationSignal.IDLE_BEHAVIOR,
            severity="medium",
            root_cause=f"{len(idle_tasks)} task(s) idle beyond {idle_threshold_sec}s: {', '.join(idle_tasks[:5])}",
            evidence={"idle_tasks": idle_tasks, "threshold_sec": idle_threshold_sec},
            suggested_remediation="Resume idle tasks or mark as blocked with a reason.",
        ))
    return diagnoses


def detect_validation_bottlenecks(
    state: dict[str, Any],
    *,
    high_attempt_threshold: int = 4,
) -> list[Diagnosis]:
    """Detect tasks with excessive validation retry attempts."""
    diagnoses: list[Diagnosis] = []
    bottleneck_tasks: list[str] = []
    for task_id, task_data in state.items():
        if not isinstance(task_data, dict):
            continue
        attempts = 0
        try:
            attempts = int(task_data.get("attempts", 0) or 0)
        except (ValueError, TypeError):
            continue
        if attempts >= high_attempt_threshold:
            bottleneck_tasks.append(str(task_id))
    if bottleneck_tasks:
        diagnoses.append(Diagnosis(
            signal=DegradationSignal.VALIDATION_BOTTLENECK,
            severity="high",
            root_cause=f"{len(bottleneck_tasks)} task(s) with {high_attempt_threshold}+ attempts: {', '.join(bottleneck_tasks[:5])}",
            evidence={"bottleneck_tasks": bottleneck_tasks, "threshold": high_attempt_threshold},
            suggested_remediation="Review validation commands for flaky tests; reduce scope.",
        ))
    return diagnoses


def detect_heartbeat_staleness(
    heartbeat_age_sec: int,
    *,
    stale_threshold_sec: int = 300,
) -> list[Diagnosis]:
    """Detect stale heartbeat indicating runner may be hung."""
    if heartbeat_age_sec < 0:
        return [Diagnosis(
            signal=DegradationSignal.HEARTBEAT_STALE,
            severity="high",
            root_cause="No heartbeat found; runner may not be running.",
            evidence={"heartbeat_age_sec": heartbeat_age_sec},
            suggested_remediation="Check if autonomy runner is alive; restart if needed.",
        )]
    if heartbeat_age_sec > stale_threshold_sec:
        return [Diagnosis(
            signal=DegradationSignal.HEARTBEAT_STALE,
            severity="high",
            root_cause=f"Heartbeat is {heartbeat_age_sec}s old (threshold: {stale_threshold_sec}s).",
            evidence={"heartbeat_age_sec": heartbeat_age_sec, "threshold_sec": stale_threshold_sec},
            suggested_remediation="Restart the autonomy runner; check for hangs.",
        )]
    return []


def detect_budget_issues(budget: dict[str, Any]) -> list[Diagnosis]:
    """Detect budget-related degradation signals."""
    diagnoses: list[Diagnosis] = []
    if not isinstance(budget, dict):
        return diagnoses
    total_cost = 0.0
    budget_limit = 0.0
    try:
        total_cost = float(budget.get("total_cost_usd", 0.0) or 0.0)
        budget_limit = float(budget.get("limit_usd", 0.0) or 0.0)
    except (ValueError, TypeError):
        return diagnoses
    if budget_limit > 0 and total_cost >= budget_limit:
        diagnoses.append(Diagnosis(
            signal=DegradationSignal.BUDGET_EXCEEDED,
            severity="critical",
            root_cause=f"Budget exhausted: ${total_cost:.2f} / ${budget_limit:.2f}.",
            evidence={"total_cost_usd": total_cost, "limit_usd": budget_limit},
            suggested_remediation="Increase budget or switch to local-only execution.",
        ))
    elif budget_limit > 0 and total_cost >= budget_limit * 0.9:
        diagnoses.append(Diagnosis(
            signal=DegradationSignal.BUDGET_EXCEEDED,
            severity="high",
            root_cause=f"Budget nearly exhausted: ${total_cost:.2f} / ${budget_limit:.2f} (>90%).",
            evidence={"total_cost_usd": total_cost, "limit_usd": budget_limit},
            suggested_remediation="Consider switching to local models to preserve budget.",
        ))
    return diagnoses


# ---------------------------------------------------------------------------
# Remediation delegation
# ---------------------------------------------------------------------------

_REMEDIATION_COUNTER = 0


def _next_remediation_id() -> str:
    global _REMEDIATION_COUNTER
    _REMEDIATION_COUNTER += 1
    return f"rem-{_REMEDIATION_COUNTER:04d}"


def create_remediations(diagnoses: list[Diagnosis]) -> list[RemediationTask]:
    """Generate remediation tasks from diagnoses."""
    tasks: list[RemediationTask] = []
    for diag in diagnoses:
        if diag.severity in ("high", "critical"):
            priority = 0 if diag.severity == "critical" else 1
            target_lane = "L0" if diag.severity == "critical" else "L1"
            tasks.append(RemediationTask(
                task_id=_next_remediation_id(),
                target_lane=target_lane,
                description=diag.suggested_remediation or f"Remediate: {diag.root_cause}",
                diagnosis_signal=diag.signal,
                priority=priority,
            ))
    return tasks


# ---------------------------------------------------------------------------
# Health scoring
# ---------------------------------------------------------------------------

def compute_health_score(diagnoses: list[Diagnosis]) -> int:
    """Compute a 0-100 health score from diagnosis list.  100 = fully healthy."""
    if not diagnoses:
        return 100
    penalty = 0
    for diag in diagnoses:
        if diag.severity == "critical":
            penalty += 40
        elif diag.severity == "high":
            penalty += 25
        elif diag.severity == "medium":
            penalty += 10
        else:
            penalty += 5
    return max(0, 100 - penalty)


def grade_from_score(score: int) -> str:
    if score >= 80:
        return HealthGrade.HEALTHY
    elif score >= 50:
        return HealthGrade.DEGRADED
    else:
        return HealthGrade.CRITICAL


# ---------------------------------------------------------------------------
# Main monitor entry point
# ---------------------------------------------------------------------------

class CollaborationHealthMonitor:
    """Continuous monitoring system for collaboration health.

    Aggregates multiple degradation detectors, produces diagnoses, computes
    an overall health grade, and delegates remediation tasks.
    """

    def __init__(
        self,
        *,
        output_dir: Path | None = None,
        stale_threshold_sec: int = 300,
        idle_threshold_sec: int = 1800,
        high_attempt_threshold: int = 4,
    ) -> None:
        self._output_dir = output_dir
        self._stale_threshold = stale_threshold_sec
        self._idle_threshold = idle_threshold_sec
        self._attempt_threshold = high_attempt_threshold
        self._last_status: HealthStatus | None = None

    @property
    def last_status(self) -> HealthStatus | None:
        return self._last_status

    def check(
        self,
        *,
        state: dict[str, Any],
        heartbeat_age_sec: int = -1,
        budget: dict[str, Any] | None = None,
    ) -> HealthStatus:
        """Run all detectors and return a HealthStatus."""
        diagnoses: list[Diagnosis] = []
        diagnoses.extend(detect_lane_stoppages(state))
        diagnoses.extend(detect_idle_behavior(state, idle_threshold_sec=self._idle_threshold))
        diagnoses.extend(detect_validation_bottlenecks(state, high_attempt_threshold=self._attempt_threshold))
        diagnoses.extend(detect_heartbeat_staleness(heartbeat_age_sec, stale_threshold_sec=self._stale_threshold))
        if budget:
            diagnoses.extend(detect_budget_issues(budget))

        score = compute_health_score(diagnoses)
        grade = grade_from_score(score)
        remediations = create_remediations(diagnoses)

        # Build task state summary
        task_statuses: dict[str, int] = {}
        for task_id, task_data in state.items():
            if isinstance(task_data, dict):
                s = str(task_data.get("status", "unknown")).strip().lower()
            else:
                s = "unknown"
            task_statuses[s] = task_statuses.get(s, 0) + 1

        status = HealthStatus(
            grade=grade,
            score=score,
            diagnoses=diagnoses,
            remediations=remediations,
            details={
                "task_status_summary": task_statuses,
                "heartbeat_age_sec": heartbeat_age_sec,
                "detector_config": {
                    "stale_threshold_sec": self._stale_threshold,
                    "idle_threshold_sec": self._idle_threshold,
                    "high_attempt_threshold": self._attempt_threshold,
                },
            },
        )

        self._last_status = status

        # Persist if output dir configured
        if self._output_dir:
            self._write_status(status)

        return status

    def _write_status(self, status: HealthStatus) -> None:
        if not self._output_dir:
            return
        self._output_dir.mkdir(parents=True, exist_ok=True)
        health_file = self._output_dir / "collaboration_health.json"
        health_file.write_text(
            json.dumps(status.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# CLI-friendly function for dashboard-status
# ---------------------------------------------------------------------------

def dashboard_health_status(
    *,
    state_file: Path,
    heartbeat_age_sec: int = -1,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a health status payload suitable for dashboard display."""
    state: dict[str, Any] = {}
    if state_file.exists():
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = raw
        except Exception:
            pass

    monitor = CollaborationHealthMonitor()
    status = monitor.check(state=state, heartbeat_age_sec=heartbeat_age_sec, budget=budget)
    return status.to_dict()

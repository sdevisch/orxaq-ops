from __future__ import annotations

import fcntl
import hashlib
import json
import random
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REQUIRED_EVENT_FIELDS = {
    "event_id",
    "timestamp",
    "event_type",
    "work_id",
    "task_type",
    "risk_level",
    "lane",
    "provider",
    "model",
    "status",
}


class TelemetrySink(ABC):
    @abstractmethod
    def emit(self, event: dict[str, Any]) -> None:
        raise NotImplementedError


class FileJsonlSink(TelemetrySink):
    """Append-only JSONL sink with process-safe file locking."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        validate_event(event)
        line = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.write(line)
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class LaneDecision:
    lane: str
    reason: str
    randomized: bool


def validate_event(event: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_EVENT_FIELDS.difference(event.keys()))
    if missing:
        raise ValueError(f"missing required fields: {','.join(missing)}")


def build_event(
    *,
    event_type: str,
    work_id: str,
    task_type: str,
    risk_level: str,
    lane: str,
    provider: str,
    model: str,
    status: str,
    duration_ms: int | None = None,
    cost_usd: float | None = None,
    reason: str = "",
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    event_id = hashlib.sha256(f"{work_id}:{event_type}:{now}".encode("utf-8")).hexdigest()[:16]
    payload: dict[str, Any] = {
        "event_id": event_id,
        "timestamp": now,
        "event_type": event_type,
        "work_id": work_id,
        "task_type": task_type,
        "risk_level": risk_level,
        "lane": lane,
        "provider": provider,
        "model": model,
        "status": status,
        "reason": reason,
    }
    if duration_ms is not None:
        payload["duration_ms"] = int(duration_ms)
    if cost_usd is not None:
        payload["cost_usd"] = float(cost_usd)
    validate_event(payload)
    return payload


def deterministic_lane_randomizer(
    *,
    work_id: str,
    task_type: str,
    risk_level: str,
    seed: int = 17,
    control_lane: str = "L0",
    treatment_lane: str = "L2",
) -> LaneDecision:
    if risk_level.lower() != "low":
        return LaneDecision(lane=control_lane, reason="risk_not_low", randomized=False)
    digest = hashlib.sha256(f"{seed}:{task_type}:{work_id}".encode("utf-8")).hexdigest()
    bit = int(digest[:8], 16) % 2
    lane = treatment_lane if bit else control_lane
    return LaneDecision(lane=lane, reason="deterministic_randomized", randomized=True)


def read_events(path: str | Path) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        event = json.loads(text)
        validate_event(event)
        out.append(event)
    return out


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def lane_ate(events: list[dict[str, Any]], *, control_lane: str = "L0", treatment_lane: str = "L2") -> dict[str, Any]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for event in events:
        if event.get("event_type") != "task_end":
            continue
        task_type = str(event.get("task_type", "unknown"))
        lane = str(event.get("lane", ""))
        grouped.setdefault(task_type, {}).setdefault(lane, []).append(event)

    by_task: dict[str, Any] = {}
    for task_type, lane_groups in sorted(grouped.items()):
        control = lane_groups.get(control_lane, [])
        treatment = lane_groups.get(treatment_lane, [])

        c_success = [1.0 if str(item.get("status", "")) == "success" else 0.0 for item in control]
        t_success = [1.0 if str(item.get("status", "")) == "success" else 0.0 for item in treatment]
        c_dur = [float(item.get("duration_ms", 0.0)) for item in control if "duration_ms" in item]
        t_dur = [float(item.get("duration_ms", 0.0)) for item in treatment if "duration_ms" in item]
        c_cost = [float(item.get("cost_usd", 0.0)) for item in control if "cost_usd" in item]
        t_cost = [float(item.get("cost_usd", 0.0)) for item in treatment if "cost_usd" in item]

        ate_success = (_avg(t_success) or 0.0) - (_avg(c_success) or 0.0)
        ate_duration_ms = None if not c_dur and not t_dur else (_avg(t_dur) or 0.0) - (_avg(c_dur) or 0.0)
        ate_cost_usd = None if not c_cost and not t_cost else (_avg(t_cost) or 0.0) - (_avg(c_cost) or 0.0)

        by_task[task_type] = {
            "n_control": len(control),
            "n_treatment": len(treatment),
            "ate_success": round(ate_success, 6),
            "ate_duration_ms": None if ate_duration_ms is None else round(ate_duration_ms, 3),
            "ate_cost_usd": None if ate_cost_usd is None else round(ate_cost_usd, 6),
        }

    return {
        "control_lane": control_lane,
        "treatment_lane": treatment_lane,
        "task_types": by_task,
    }


def seeded_shuffle(values: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    copy = list(values)
    rng.shuffle(copy)
    return copy
